#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
财税公告监管 —— 抓取端（GitHub Actions 定时运行）

流程：读 sources.yaml → 抓列表 → 与 seen.json 比对出新增 → 抓详情页正文
      → 调 LLM 结构化提取 → POST 到 Hermes Webhook

设计约束：
  * 不处理任何登录/Cookie 源，type: auth-cookie 直接跳过
  * 单源、单条失败只打日志，不影响其它源
  * 所有密钥只从环境变量读取，绝不落盘
  * seen.json 只记录已成功推送的条目，推送失败的下轮自动重试

推送的 JSON 字段固定为（schema_version 1.0）：
  schema_version, event_id, source, source_name, type, url, title,
  published_at, fetched_at, meta{}, doc{}, text_excerpt, llm_model, llm_ok

用法：
  python watch.py --dry-run            # 只抓取和解析，不调 LLM、不推送、不写 seen
  python watch.py --no-llm             # 抓取+推送，但不调 LLM（doc 留空壳）
  python watch.py --source 财政部-政策发布 --limit 3
  python watch.py --list-sources
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import random
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
SCHEMA_VERSION = "1.0"

UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.2 Safari/605.1.15",
]

log = logging.getLogger("tax-watch")


# --------------------------------------------------------------------------- 工具
def now_local_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def norm_url(url: str) -> str:
    """去 fragment / 去 tracking 参数，作为去重键。"""
    p = urlsplit(url.strip())
    query = "&".join(
        kv for kv in p.query.split("&") if kv and not kv.lower().startswith(("utm_", "spm"))
    )
    return urlunsplit((p.scheme, p.netloc.lower(), p.path, query, ""))


def event_id_for(source_name: str, url: str) -> str:
    return hashlib.sha1(f"{source_name}|{norm_url(url)}".encode("utf-8")).hexdigest()[:16]


def clean_text(s: str) -> str:
    s = re.sub(r"[ \t\u00a0\u3000]+", " ", s or "")
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def guess_date(text: str) -> str:
    m = re.search(r"(20\d{2})\s*[-/年.]\s*(\d{1,2})\s*[-/月.]\s*(\d{1,2})", text or "")
    if not m:
        return ""
    y, mo, d = (int(x) for x in m.groups())
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return ""
    return f"{y:04d}-{mo:02d}-{d:02d}"


def parse_iso_date(value: str | None) -> date | None:
    """把 'YYYY-MM-DD' / 'YYYY年M月D日' / 毫秒时间戳 之类尽量解析成 date。"""
    s = (value or "").strip()
    if not s:
        return None
    if s.isdigit() and len(s) in (12, 13):      # 毫秒时间戳
        try:
            return datetime.fromtimestamp(int(s) / 1000, tz=timezone.utc).astimezone().date()
        except Exception:  # noqa: BLE001
            return None
    g = guess_date(s)
    if not g:
        return None
    try:
        y, m, d = (int(x) for x in g.split("-"))
        return date(y, m, d)
    except ValueError:
        return None


def days_until(target: date | None, today: date | None = None) -> int | None:
    if target is None:
        return None
    return (target - (today or date.today())).days


class Fetcher:
    """带随机延时、UA 轮换、重试的请求器。"""

    def __init__(self, delay: tuple[float, float] = (1.5, 4.0), timeout: int = 30,
                 verify_ssl: bool = True):
        self.session = requests.Session()
        self.delay = delay
        self.timeout = timeout
        self.verify = verify_ssl
        self._last = 0.0

    def _sleep(self) -> None:
        gap = random.uniform(*self.delay) - (time.time() - self._last)
        if gap > 0:
            time.sleep(gap)
        self._last = time.time()

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        headers = {
            "User-Agent": random.choice(UA_POOL),
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        }
        headers.update(kwargs.pop("headers", {}) or {})
        last_err: Exception | None = None
        for attempt in range(3):
            self._sleep()
            try:
                resp = self.session.request(
                    method, url, headers=headers, timeout=self.timeout,
                    verify=self.verify, allow_redirects=True, **kwargs
                )
                if resp.status_code in (403, 412, 429) and attempt < 2:
                    log.warning("  %s → HTTP %s，%d 秒后重试", url, resp.status_code, 3 * (attempt + 1))
                    time.sleep(3 * (attempt + 1))
                    continue
                if resp.status_code >= 400:
                    raise requests.HTTPError(f"HTTP {resp.status_code}")
                if not resp.encoding or resp.encoding.lower() in ("iso-8859-1", "ascii"):
                    resp.encoding = resp.apparent_encoding or "utf-8"
                return resp
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                log.warning("  请求失败（第 %d 次）：%s | %s", attempt + 1, url, exc)
                time.sleep(2 * (attempt + 1))
        raise RuntimeError(f"请求彻底失败：{url} | {last_err}") from last_err


# ---------------------------------------------------------------------- 列表解析
def collect_meta(domain_meta_list: list[dict]) -> dict[str, str]:
    """把法规库接口返回的 domainMetaList 摊平成 {中文名: 值}。"""
    meta: dict[str, str] = {}
    for group in domain_meta_list or []:
        for item in group.get("resultList") or []:
            val = (item.get("value") or "").strip()
            if val:
                meta[item.get("name") or item.get("key", "")] = val
    return meta


def items_from_json_api(src: dict, fetcher: Fetcher) -> list[dict]:
    api = src.get("api") or {}
    endpoint = api.get("endpoint")
    if not endpoint:
        raise ValueError("mode=json_api 但缺少 api.endpoint")

    collected: dict[str, dict] = {}
    for page in range(1, int(api.get("max_pages", 1)) + 1):
        payload = {
            "channelId": api.get("channel_id", ""),
            "codeId": api.get("code_id", ""),
            "page": page,
            "size": int(api.get("page_size", 20)),
            "relateSubChannels": "false",
        }
        method = (api.get("method") or "POST").upper()
        headers = {"Referer": src.get("url", endpoint),
                   "X-Requested-With": "XMLHttpRequest"}
        if method == "GET":
            resp = fetcher.request("GET", endpoint, params=payload, headers=headers)
        else:
            resp = fetcher.request("POST", endpoint, data=payload, headers=headers)

        try:
            body = resp.json()
            data = (body.get("results") or {}).get("data") or {}
            rows = data.get("results") or []
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"接口返回不是预期 JSON：{exc}") from exc
        if not rows:
            break
        for row in rows:
            url = (row.get("url") or row.get("redirectUrl") or "").strip()
            title = (row.get("title") or "").strip()
            if not url or not title:
                continue
            url = absolutize(url, src)
            collected.setdefault(norm_url(url), {
                "title": title,
                "url": url,
                "date": (row.get("publishedTimeStr") or "")[:10] or guess_date(title),
                "meta": collect_meta(row.get("domainMetaList")),
            })
    return list(collected.values())


def items_from_html(src: dict, fetcher: Fetcher) -> list[dict]:
    url = src.get("url")
    if not url:
        raise ValueError("mode=html 但缺少 url")
    resp = fetcher.request("GET", url)
    soup = BeautifulSoup(resp.text, "lxml")

    list_sel = src.get("list_selector")
    title_sel = src.get("title_selector") or "a"
    link_sel = src.get("link_selector") or "a"
    date_sel = src.get("date_selector")

    nodes = soup.select(list_sel) if list_sel else []
    if not nodes:
        log.warning("  list_selector=%r 没匹配到条目，跳过", list_sel)
        return []

    out: dict[str, dict] = {}
    for node in nodes:
        a = node.select_one(title_sel)
        if a is None:
            continue
        title = clean_text(a.get_text(" ", strip=True))
        link_el = node.select_one(link_sel) or a
        href = link_el.get("href") or ""
        if not title or not href or href.startswith(("javascript:", "#")):
            continue
        date = ""
        if date_sel:
            d_el = node.select_one(date_sel)
            if d_el:
                date = guess_date(d_el.get_text(" ", strip=True))
        if not date:
            date = guess_date(node.get_text(" ", strip=True))
        if not date:                       # 有些站用 13 位毫秒时间戳代替日期
            m = re.search(r"\b(1\d{12})\b", node.get_text(" ", strip=True))
            if m:
                parsed = parse_iso_date(m.group(1))
                date = parsed.isoformat() if parsed else ""
        full = absolutize(href, src)
        out.setdefault(norm_url(full), {
            "title": title, "url": full, "date": date, "meta": {},
        })
    return list(out.values())


def absolutize(href: str, src: dict) -> str:
    href = (href or "").strip()
    if not href:
        return ""
    base = src.get("base_url") or src.get("url") or ""
    full = urljoin(base if base.endswith("/") else base + "/", href) if not href.startswith("http") else href
    rewrite = (src.get("url_host_rewrite") or "").strip()
    if rewrite:
        p = urlsplit(full)
        base_scheme = urlsplit(base).scheme or "https"
        full = urlunsplit((base_scheme, rewrite, p.path, p.query, ""))
    return full


# 法规库接口自带的元数据比 LLM 更可靠，用来回填空字段
META_TO_DOC = {
    "发文字号": "doc_no",
    "发文单位": "issuer",
    "发文机构": "issuer",
    "成文日期": "signed_date",
    "时效性": "effect_status",
    "效力等级": "authority_level",
}


def merge_meta_into_doc(doc: dict, meta: dict) -> dict:
    for meta_key, doc_key in META_TO_DOC.items():
        val = (meta or {}).get(meta_key, "").strip()
        if val and not (doc.get(doc_key) or "").strip():
            doc[doc_key] = val
    if not doc.get("doc_type") and (meta or {}).get("效力等级"):
        doc["doc_type"] = meta["效力等级"]
    if not doc.get("business_tags") and (meta or {}).get("主题标签"):
        doc["business_tags"] = [t.strip() for t in meta["主题标签"].split(",") if t.strip()]
    return doc


# ------------------------------------------------------------------ 详情页正文
CONTENT_SELECTORS = [
    "div.TRS_Editor", "div.arc_cont", "div#zoom", "div.article-content",
    "div.content", "div#content", "div.article", "article", "div.main-txt",
]


def fetch_detail(url: str, src: dict, fetcher: Fetcher, limit: int = 6000) -> dict:
    """返回 {'text': 正文, 'attachments': [{'name','url'}]}"""
    result = {"text": "", "attachments": []}
    try:
        resp = fetcher.request("GET", url)
    except Exception as exc:  # noqa: BLE001
        log.warning("  详情页取不到：%s | %s", url, exc)
        return result
    soup = BeautifulSoup(resp.text, "lxml")
    for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form"]):
        tag.decompose()

    selectors = [src["content_selector"]] if src.get("content_selector") else []
    selectors += CONTENT_SELECTORS
    container = None
    for sel in selectors:
        el = soup.select_one(sel)
        if el is None:
            continue
        text = clean_text(el.get_text("\n", strip=True))
        if len(text) >= 120:
            container, result["text"] = el, text[:limit]
            break

    if container is None:
        # 启发式兜底：取最长的叶子块
        best_el, best_txt = None, ""
        for el in soup.find_all(["div", "td", "article", "section"]):
            if el.find(["div", "article", "section"]):
                continue
            t = clean_text(el.get_text("\n", strip=True))
            if len(t) > len(best_txt):
                best_el, best_txt = el, t
        container, result["text"] = best_el, best_txt[:limit]

    # 附件（PDF/Word/Excel），公告里经常写「具体清单见附件」
    scope = container or soup
    seen: set[str] = set()
    for a in scope.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("javascript:", "#", "mailto:")):
            continue
        low = href.lower()
        if not any(low.endswith(ext) for ext in (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".zip")) \
                and "/files/" not in low:
            continue
        full = urljoin(url, href)
        if full in seen:
            continue
        seen.add(full)
        name = clean_text(a.get_text(" ", strip=True)) or full.rsplit("/", 1)[-1]
        result["attachments"].append({"name": name[:100], "url": full})
    return result


# --------------------------------------------------------------------- LLM 提取
EMPTY_DOC = {
    "title_clean": "", "doc_no": "", "issuer": "", "doc_type": "",
    "signed_date": "", "effective_date": "", "expire_date": "", "abolish_note": "",
    "business_tags": [], "summary": "", "key_changes": [], "scenarios": [],
    "affected": "", "is_relevant": True, "risk_level": "低", "need_read_full": False,
    "stage": "", "comment_deadline": "",
}


def load_prompt(path: Path) -> str:
    if path.exists():
        return path.read_text(encoding="utf-8")
    return "你是财税政策分析助手，只输出 JSON。"


def parse_json_lenient(text: str) -> dict:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            return json.loads(m.group(0))
        raise


def extract_with_llm(prompt: str, item: dict, text: str, cfg: dict) -> tuple[dict, str, bool]:
    base = (os.environ.get("LLM_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
    key = os.environ.get("LLM_KEY") or os.environ.get("OPENAI_API_KEY") or ""
    model = os.environ.get("LLM_MODEL") or "gpt-4o-mini"
    if not key:
        log.warning("  未配置 LLM_KEY，跳过 LLM 提取")
        return dict(EMPTY_DOC), model, False

    user_msg = (
        f"来源：{item.get('source_name', '')}\n"
        f"标题：{item['title']}\n"
        f"公告页日期：{item.get('date') or '未知'}\n"
        f"列表页元数据：{json.dumps(item.get('meta') or {}, ensure_ascii=False)}\n"
        f"链接：{item['url']}\n\n"
        f"正文：\n{text[:6000]}"
    )
    body = {
        "model": model,
        "temperature": 0.1,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": user_msg},
        ],
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    last_err: Exception | None = None
    for attempt in range(3):
        try:
            resp = requests.post(f"{base}/chat/completions", json=body, headers=headers,
                                 timeout=120)
            if resp.status_code == 400 and "response_format" in resp.text:
                body.pop("response_format", None)
                continue
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            doc = {**EMPTY_DOC, **parse_json_lenient(content)}
            return doc, model, True
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            log.warning("  LLM 调用失败（第 %d 次）：%s", attempt + 1, exc)
            time.sleep(3 * (attempt + 1))
    log.error("  LLM 提取放弃：%s", last_err)
    return dict(EMPTY_DOC), model, False


# ------------------------------------------------------------------------ 推送
def push_webhook(event: dict, outbox: Path, dry_run: bool) -> bool:
    url = os.environ.get("HERMES_WEBHOOK_URL", "").strip()
    token = os.environ.get("HERMES_WEBHOOK_TOKEN", "").strip()
    if dry_run:
        log.info("  [dry-run] 不推送：%s", event["title"])
        return True
    if not url:
        # 没配 webhook：走 outbox-only 模式（Actions 在公网，本来就 POST 不到内网）
        # 事件落到仓库 outbox/，由 Hermes 侧 pull_outbox.py 定时补发
        write_outbox(event, outbox)
        log.info("  未配置 webhook，已写入 outbox 待 Hermes 拉取")
        return True

    headers = {"Content-Type": "application/json",
               "X-Watch-Token": token or "", "X-Watch-Event": event["event_id"]}
    for attempt in range(3):
        try:
            resp = requests.post(url, json=event, headers=headers, timeout=30)
            if resp.status_code < 300:
                log.info("  推送成功：%s", event["title"][:40])
                return True
            log.warning("  Webhook 返回 %s：%s", resp.status_code, resp.text[:200])
        except Exception as exc:  # noqa: BLE001
            log.warning("  Webhook 异常（第 %d 次）：%s", attempt + 1, exc)
        time.sleep(3 * (attempt + 1))

    write_outbox(event, outbox)
    return False


def write_outbox(event: dict, outbox: Path) -> None:
    outbox.mkdir(parents=True, exist_ok=True)
    path = outbox / f"{event['event_id']}.json"
    path.write_text(json.dumps(event, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("  已存待重放：%s", path.name)


# ------------------------------------------------------------------- 状态文件
def load_seen(path: Path) -> dict:
    if not path.exists():
        return {"version": 1, "sources": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        data.setdefault("sources", {})
        return data
    except Exception:  # noqa: BLE001
        log.warning("seen.json 损坏，重新开始（旧记录备份为 seen.json.bak）")
        path.replace(path.with_suffix(".json.bak"))
        return {"version": 1, "sources": {}}


def save_seen(path: Path, seen: dict) -> None:
    # 每个源只保留最近 800 条，防止文件无限膨胀
    for name, bucket in seen["sources"].items():
        urls = bucket.get("urls") or {}
        if len(urls) > 800:
            keep = sorted(urls.items(), key=lambda kv: kv[1].get("seen_at", ""), reverse=True)[:800]
            bucket["urls"] = dict(keep)
    path.write_text(json.dumps(seen, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                    encoding="utf-8")


# ------------------------------------------------------- 生效日历（将来才实施的）
def load_calendar(path: Path) -> dict:
    """calendar.json：所有「还未生效」的条目，供 Hermes 端做倒计时提醒。"""
    if not path.exists():
        return {"version": 1, "items": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        data.setdefault("items", {})
        return data
    except Exception:  # noqa: BLE001
        log.warning("calendar.json 损坏，重新开始")
        return {"version": 1, "items": {}}


def save_calendar(path: Path, cal: dict, today: date | None = None) -> None:
    today = today or date.today()
    items = cal.get("items") or {}
    # 生效日已过去 7 天以上的条目清掉，文件不会无限长
    for eid in list(items):
        eff = parse_iso_date(items[eid].get("effective_date", ""))
        if eff is None or (eff - today).days < -7:
            items.pop(eid, None)
    cal["items"] = items
    cal["updated_at"] = now_local_iso()
    path.write_text(json.dumps(cal, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                    encoding="utf-8")


# ------------------------------------------------------------------------ 主流程
def keyword_ok(title: str, src: dict) -> bool:
    kws = src.get("keywords") or []
    ex = src.get("exclude_keywords") or []
    if any(k in title for k in ex):
        return False
    if kws and not any(k in title for k in kws):
        return False
    return True


def process_source(src: dict, seen: dict, defaults: dict, args: argparse.Namespace,
                   fetcher: Fetcher, prompt: str, outbox: Path,
                   calendar: dict | None = None) -> tuple[int, int]:
    name = src.get("name") or "未命名源"

    if not src.get("enabled", True):
        log.info("[跳过] %s（enabled: false）", name)
        return 0, 0
    if (src.get("type") or "public") != "public":
        log.info("[跳过] %s（type: %s，需人工登录，不自动处理）", name, src.get("type"))
        return 0, 0
    if args.source and args.source != name:
        return 0, 0

    log.info("[抓取] %s", name)
    try:
        if (src.get("mode") or "html") == "json_api":
            items = items_from_json_api(src, fetcher)
        else:
            items = items_from_html(src, fetcher)
    except Exception as exc:  # noqa: BLE001
        log.error("[失败] %s 列表抓取异常：%s", name, exc)
        return 0, 0

    log.info("  列表共 %d 条（含历史）", len(items))
    bucket = seen["sources"].setdefault(name, {"urls": {}})
    known: dict = bucket.setdefault("urls", {})

    fresh = [it for it in items if norm_url(it["url"]) not in known and keyword_ok(it["title"], src)]
    # 关键：按发布日期从新到旧挑，绝不按列表顺序（列表顺序常常是栏目顺序，会先吐最旧的积压）
    fresh.sort(key=lambda it: (it.get("date") or ""), reverse=True)
    limit = args.limit or int(src.get("max_new_per_source", defaults.get("max_new_per_source", 8)))
    fresh = fresh[:limit]
    log.info("  新增待处理 %d 条（本次上限 %d，已按日期倒序）", len(fresh), limit)

    max_age = int(src.get("max_age_days") or defaults.get("max_age_days", 30))
    draft_age = int(src.get("draft_max_age_days") or defaults.get("draft_max_age_days", 45))
    forward = int(defaults.get("forward_window_days", 120))
    today = date.today()

    pushed = 0
    for it in fresh:
        it["source_name"] = name
        log.info("  → %s", it["title"][:60])
        detail = fetch_detail(it["url"], src, fetcher)
        text = detail["text"]
        if detail["attachments"]:
            log.info("    附件 %d 个：%s", len(detail["attachments"]),
                     "、".join(a["name"] for a in detail["attachments"][:3]))
        if len(text) < 80:
            log.warning("    正文过短（%d 字），仍推送标题与元数据", len(text))

        if args.no_llm:
            doc, model, llm_ok = dict(EMPTY_DOC), "", False
        else:
            doc, model, llm_ok = extract_with_llm(prompt, it, text, src)
        # 接口自带的文号/成文日期/效力等级回填空字段（比 LLM 更准）
        doc = merge_meta_into_doc(doc, it.get("meta") or {})

        # ---- 判定「值不值得推」：新出台 / 即将生效 / 正在征求意见，其余一律静默归档
        stage = (src.get("stage") or "").strip() or (doc.get("stage") or "").strip() or ""
        if not stage:
            stage = "征求意见中" if "征求意见" in f"{it['title']}{doc.get('title_clean') or ''}" else "已发布"
        signed = parse_iso_date(doc.get("signed_date")) or parse_iso_date(it.get("date"))
        eff = parse_iso_date(doc.get("effective_date"))
        pub_age = (today - signed).days if signed else None
        eff_in = days_until(eff, today)
        effective_soon = eff_in is not None and 0 <= eff_in <= forward
        fresh_news = pub_age is not None and pub_age <= max_age
        fresh_draft = stage == "征求意见中" and (pub_age is None or pub_age <= draft_age)

        if not (fresh_news or fresh_draft or effective_soon):
            log.info("    已过时效（成文 %s，%s 天前，生效 %s），静默归档不推送",
                     doc.get("signed_date") or it.get("date") or "未知",
                     pub_age if pub_age is not None else "?", doc.get("effective_date") or "-")
            known[norm_url(it["url"])] = {
                "url": it["url"], "title": it["title"],
                "seen_at": now_local_iso(), "pushed": False, "skip": "stale",
            }
            continue
        doc["stage"] = stage

        if not doc.get("is_relevant", True):
            log.info("    LLM 判定与本业务无关，标记已读不推送")
            known[norm_url(it["url"])] = {
                "url": it["url"], "title": it["title"],
                "seen_at": now_local_iso(), "pushed": False, "skip": "irrelevant",
            }
            continue

        event = {
            "schema_version": SCHEMA_VERSION,
            "event_id": event_id_for(name, it["url"]),
            "source": name,
            "source_name": name,
            "type": src.get("type", "public"),
            "url": it["url"],
            "title": it["title"],
            "published_at": it.get("date") or doc.get("signed_date") or "",
            "fetched_at": now_local_iso(),
            "meta": it.get("meta") or {},
            "doc": doc,
            "attachments": detail["attachments"][:6],
            "text_excerpt": text[:3000],
            "llm_model": model,
            "llm_ok": llm_ok,
            "stage": stage,
            "effective_date": doc.get("effective_date") or "",
        }

        ok = push_webhook(event, outbox, args.dry_run)
        if args.dry_run:
            preview_dir = outbox / "dryrun"
            preview_dir.mkdir(parents=True, exist_ok=True)
            (preview_dir / f"{event['event_id']}.json").write_text(
                json.dumps(event, ensure_ascii=False, indent=2), encoding="utf-8")
            log.info("    [dry-run] 正文 %d 字 → 预览 outbox/dryrun/%s.json",
                     len(text), event["event_id"])
            log.info("    [dry-run] 解析结果：%s", json.dumps(
                {k: doc.get(k) for k in ("doc_no", "issuer", "signed_date", "effective_date",
                                         "risk_level", "summary")}, ensure_ascii=False))
        if ok:
            pushed += 1
            if not args.dry_run:
                known[norm_url(it["url"])] = {
                    "url": it["url"], "title": it["title"],
                    "seen_at": now_local_iso(), "pushed": True,
                }
                # 还没生效的，记进「生效日历」，Hermes 端据此做倒计时提醒
                if calendar is not None and eff is not None and eff >= today:
                    calendar.setdefault("items", {})[event["event_id"]] = {
                        "event_id": event["event_id"],
                        "title": doc.get("title_clean") or it["title"],
                        "doc_no": doc.get("doc_no") or "",
                        "issuer": doc.get("issuer") or "",
                        "signed_date": doc.get("signed_date") or "",
                        "effective_date": doc["effective_date"],
                        "source": name,
                        "url": it["url"],
                        "risk_level": doc.get("risk_level") or "低",
                        "added_at": now_local_iso(),
                    }
        else:
            log.warning("    推送失败，不写入 seen，下轮重试")

    bucket["last_run"] = now_local_iso()
    bucket["last_ok"] = now_local_iso()
    return len(fresh), pushed


def main() -> int:
    ap = argparse.ArgumentParser(description="财税公告监管抓取端")
    ap.add_argument("--config", default=str(ROOT / "sources.yaml"))
    ap.add_argument("--seen", default=str(ROOT / "seen.json"))
    ap.add_argument("--prompt", default=str(ROOT / "prompt.txt"))
    ap.add_argument("--outbox", default=str(ROOT / "outbox"))
    ap.add_argument("--calendar", default=str(ROOT / "calendar.json"))
    ap.add_argument("--source", help="只跑指定名称的源")
    ap.add_argument("--limit", type=int, help="覆盖单源新增上限")
    ap.add_argument("--dry-run", action="store_true", help="只抓取解析，不调 LLM/不推送/不写状态")
    ap.add_argument("--no-llm", action="store_true", help="跳过 LLM 提取")
    ap.add_argument("--list-sources", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO, stream=sys.stdout,
        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S",
    )

    config_path = Path(args.config)
    if not config_path.exists():
        log.error("找不到配置文件：%s", config_path)
        return 2
    conf = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    defaults = conf.get("defaults") or {}
    sources = conf.get("sources") or []

    if args.list_sources:
        for s in sources:
            flag = "✓" if s.get("enabled", True) and (s.get("type") or "public") == "public" else "✗"
            print(f"{flag} {s.get('name')}  [{s.get('type', 'public')}/{s.get('mode', 'html')}]")
        return 0

    seen_path = Path(args.seen)
    seen = load_seen(seen_path)
    outbox = Path(args.outbox)
    cal_path = Path(args.calendar)
    calendar = load_calendar(cal_path)
    prompt = load_prompt(Path(args.prompt))

    verify = bool(defaults.get("verify_ssl", True))
    total_new = total_pushed = 0
    failures: list[str] = []

    for src in sources:
        fetcher = Fetcher(
            delay=tuple(src.get("request_delay") or defaults.get("request_delay", (1.5, 4.0))),
            timeout=int(src.get("timeout") or defaults.get("timeout", 30)),
            verify_ssl=bool(src.get("verify_ssl", verify)),
        )
        try:
            new_n, pushed_n = process_source(src, seen, defaults, args, fetcher, prompt, outbox,
                                             calendar)
            total_new += new_n
            total_pushed += pushed_n
        except Exception as exc:  # noqa: BLE001
            log.exception("[失败] %s：%s", src.get("name"), exc)
            failures.append(src.get("name") or "?")

    if not args.dry_run:
        save_seen(seen_path, seen)
        save_calendar(cal_path, calendar)

    log.info("=" * 56)
    log.info("完成：新增 %d 条，推送 %d 条%s", total_new, total_pushed,
             "，dry-run 模式未落库" if args.dry_run else "")
    if failures:
        log.warning("失败的源：%s", "、".join(failures))
        # 有源失败但整体跑完 → 退出码 0，避免 Actions 标红干扰；严重失败才非 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
