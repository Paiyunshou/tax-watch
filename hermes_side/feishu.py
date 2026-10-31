#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
飞书推送：把 watch.py 推来的 event JSON 组装成飞书卡片并发送。

两种发送方式，按需选一种配置：
  A. 自定义机器人 Webhook（最省事，不用公网 IP）
     环境变量 FEISHU_WEBHOOK_URL，若机器人开了「签名校验」再加 FEISHU_SECRET
  B. 不动飞书 webhook，改用 Hermes gateway 发（此文件不覆盖，见 README）

本模块只做「组装 + 发送」，不写思源笔记。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from datetime import datetime

import requests

RISK_COLOR = {"高": "red", "中": "orange", "低": "blue"}
MAX_ITEMS = 5


def _h(text: str) -> str:
    """飞书 lark_md 里的 HTML 会被转义，这里只做纯文本清洗。"""
    return (text or "").replace("\r", "").strip()


def _bullets(items: list[str], prefix: str = "• ") -> str:
    items = [x for x in (items or []) if str(x).strip()][:MAX_ITEMS]
    return "\n".join(f"{prefix}{_h(str(x))}" for x in items)


def build_card(event: dict) -> dict:
    doc = event.get("doc") or {}
    risk = doc.get("risk_level") or "低"
    tags = [t for t in (doc.get("business_tags") or []) if t]
    scenarios = [s for s in (doc.get("scenarios") or []) if s and s != "不适用"]

    title = _h(doc.get("title_clean") or event.get("title") or "政策更新")
    if len(title) > 60:
        title = title[:58] + "…"

    # ---- 头部信息：文号 / 机关 / 日期
    head_lines = []
    if doc.get("doc_no"):
        head_lines.append(f"**文号**　{_h(doc['doc_no'])}")
    if doc.get("issuer"):
        head_lines.append(f"**发文机关**　{_h(doc['issuer']).replace(',', '、')}")
    dates = []
    if doc.get("signed_date"):
        dates.append(f"成文 {doc['signed_date']}")
    if doc.get("effective_date"):
        dates.append(f"生效 {doc['effective_date']}")
    if doc.get("expire_date"):
        dates.append(f"失效 {doc['expire_date']}")
    if dates:
        head_lines.append("**日期**　" + "　|　".join(dates))
    head_lines.append(f"**来源**　{_h(event.get('source', ''))}")

    elements: list[dict] = [
        {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(head_lines)}},
        {"tag": "hr"},
    ]

    summary = _h(doc.get("summary"))
    if summary:
        elements.append({"tag": "div", "text": {"tag": "lark_md",
                                                "content": f"**摘要**\n{summary}"}})

    changes = _bullets(doc.get("key_changes") or [])
    if changes:
        elements.append({"tag": "div", "text": {"tag": "lark_md",
                                                "content": f"**关键变化**\n{changes}"}})

    tail = []
    if scenarios:
        tail.append(f"**适用场景**　{'、'.join(scenarios[:6])}")
    if tags:
        tail.append(f"**标签**　{'、'.join(tags[:8])}")
    if doc.get("abolish_note"):
        tail.append(f"**废止/替代**　{_h(doc['abolish_note'])[:200]}")
    if doc.get("affected"):
        tail.append(f"**影响对象**　{_h(doc['affected'])[:150]}")
    if tail:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(tail)}})

    atts = event.get("attachments") or []
    if atts:
        lines = [f"• [{_h(a.get('name'))[:40]}]({a.get('url')})" for a in atts[:4]]
        elements.append({"tag": "div", "text": {"tag": "lark_md",
                                                "content": "**附件**\n" + "\n".join(lines)}})

    if doc.get("need_read_full"):
        elements.append({"tag": "note", "elements": [
            {"tag": "plain_text", "content": "正文可能不完整（含附件/表格），建议点开原文核对"}]})

    elements.append({"tag": "hr"})
    elements.append({"tag": "action", "actions": [{
        "tag": "button",
        "text": {"tag": "plain_text", "content": "阅读原文"},
        "type": "primary",
        "url": event.get("url", ""),
    }]})
    elements.append({"tag": "note", "elements": [{
        "tag": "plain_text",
        "content": f"抓取时间 {event.get('fetched_at', '')[:16].replace('T', ' ')}"
                   f"　·　未写入思源，确认有价值再手动归档",
    }]})

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": RISK_COLOR.get(risk, "blue"),
            "title": {"tag": "plain_text", "content": f"【{risk}】{title}"},
        },
        "elements": elements,
    }


def feishu_sign(secret: str, timestamp: str) -> str:
    string_to_sign = f"{timestamp}\n{secret}".encode("utf-8")
    digest = hmac.new(string_to_sign, b"", digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


# --------------------------------------------------------- 方式 B：应用 API 直发
def _api_base() -> str:
    """FEISHU_DOMAIN=feishu（国内版）→ open.feishu.cn；lark → open.larksuite.com"""
    domain = (os.environ.get("FEISHU_DOMAIN") or "feishu").strip().lower()
    return "https://open.larksuite.com" if domain.startswith("lark") else "https://open.feishu.cn"


def get_tenant_token() -> str:
    app_id = os.environ.get("FEISHU_APP_ID", "").strip()
    app_secret = os.environ.get("FEISHU_APP_SECRET", "").strip()
    if not (app_id and app_secret):
        return ""
    resp = requests.post(
        f"{_api_base()}/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": app_id, "app_secret": app_secret}, timeout=20,
    )
    body = resp.json() if resp.content else {}
    if body.get("code") != 0:
        print(f"[feishu] 取 tenant_access_token 失败：{str(body)[:200]}")
        return ""
    return body.get("tenant_access_token", "")


def send_via_app(card: dict) -> bool:
    """用 Hermes 已在用的飞书自建应用发消息，无需另建机器人。"""
    token = get_tenant_token()
    chat_id = (os.environ.get("FEISHU_CHAT_ID") or os.environ.get("FEISHU_HOME_CHANNEL") or "").strip()
    if not token or not chat_id:
        return False
    resp = requests.post(
        f"{_api_base()}/open-apis/im/v1/messages",
        params={"receive_id_type": "chat_id"},
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"receive_id": chat_id, "msg_type": "interactive",
              "content": json.dumps(card, ensure_ascii=False)},
        timeout=20,
    )
    body = resp.json() if resp.content else {}
    if resp.status_code < 300 and body.get("code") == 0:
        return True
    print(f"[feishu] 应用 API 发送失败 HTTP {resp.status_code}：{str(body)[:300]}")
    return False


def send_card(card: dict, dry_run: bool = False) -> bool:
    if dry_run:
        print(json.dumps({"msg_type": "interactive", "card": card}, ensure_ascii=False, indent=2))
        return True

    # 优先走飞书自建应用（Hermes 已有的那套凭证），其次才用自定义机器人 webhook
    if os.environ.get("FEISHU_APP_ID") and os.environ.get("FEISHU_APP_SECRET"):
        for attempt in range(3):
            try:
                if send_via_app(card):
                    return True
            except Exception as exc:  # noqa: BLE001
                print(f"[feishu] 应用 API 异常（第 {attempt + 1} 次）：{exc}")
            time.sleep(2 * (attempt + 1))

    url = os.environ.get("FEISHU_WEBHOOK_URL", "").strip()
    if not url:
        print("[feishu] 既没有应用凭证也没有 FEISHU_WEBHOOK_URL，跳过发送")
        return False

    payload: dict = {"msg_type": "interactive", "card": card}
    secret = os.environ.get("FEISHU_SECRET", "").strip()
    if secret:
        ts = str(int(time.time()))
        payload.update({"timestamp": ts, "sign": feishu_sign(secret, ts)})

    for attempt in range(3):
        try:
            resp = requests.post(url, json=payload, timeout=20)
            body = resp.json() if resp.content else {}
            code = body.get("code", body.get("StatusCode", 0))
            if resp.status_code < 300 and code == 0:
                return True
            print(f"[feishu] 发送失败 HTTP {resp.status_code}：{str(body)[:300]}")
        except Exception as exc:  # noqa: BLE001
            print(f"[feishu] 异常（第 {attempt + 1} 次）：{exc}")
        time.sleep(2 * (attempt + 1))
    return False


RISK_ORDER = {"高": 0, "中": 1, "低": 2}
STAGE_DRAFT = "征求意见中"


def _stage(event: dict) -> str:
    doc = event.get("doc") or {}
    return (doc.get("stage") or event.get("stage") or "").strip()


def _parse(value: str | None):
    from datetime import datetime as _dt
    try:
        return _dt.strptime((value or "")[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _split_events(events: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """把事件分成：新发布 / 老文件但将来才生效 / 征求意见中。"""
    from datetime import date as _date
    today = _date.today()
    news, pending, drafts = [], [], []
    for e in events:
        doc = e.get("doc") or {}
        stage = _stage(e)
        signed = _parse(doc.get("signed_date"))
        eff = _parse(doc.get("effective_date"))
        if stage == STAGE_DRAFT:
            deadline = _parse(doc.get("comment_deadline"))
            if deadline is not None and (today - deadline).days > 0:
                continue                       # 征集期已过，没价值了
            drafts.append(e)
        elif eff is not None and (eff - today).days >= 0 and (
                signed is None or (today - signed).days > 45):
            pending.append(e)                  # 早就发布、还没生效
        else:
            news.append(e)
    return news, pending, drafts


def _item_block(i: int, e: dict) -> dict:
    doc = e.get("doc") or {}
    head = _h(doc.get("title_clean") or e.get("title") or "")
    lines = [f"**{i}. [{head}]({e.get('url')})**"]
    meta = []
    if doc.get("doc_no"):
        meta.append(_h(doc["doc_no"]))
    if e.get("source"):
        meta.append(_h(e["source"]))
    if doc.get("signed_date"):
        meta.append(f"成文 {doc['signed_date']}")
    if doc.get("effective_date"):
        meta.append(f"生效 {doc['effective_date']}")
    meta.append(f"风险 {doc.get('risk_level') or '低'}")
    lines.append("　·　".join(meta))
    if doc.get("summary"):
        lines.append(_h(doc["summary"])[:130])
    return {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}}


def _section_title(text: str) -> dict:
    return {"tag": "div", "text": {"tag": "lark_md", "content": f"**【{text}】**"}}


def build_digest_card(events: list[dict], date_label: str = "", max_items: int = 8,
                      upcoming: list[dict] | None = None) -> dict:
    """日报卡：① 新政速览 ② 即将生效 ③ 征求意见中。"""
    events = events or []
    news, pending, drafts = _split_events(events)
    # 先按日期从新到旧，再按风险稳定排序（高风险在前）
    news.sort(key=lambda e: str((e.get("doc") or {}).get("signed_date")
                                or e.get("published_at") or ""), reverse=True)
    news.sort(key=lambda e: RISK_ORDER.get(((e.get("doc") or {}).get("risk_level") or "低"), 3))

    # 「即将生效」= 已发布但还没生效的旧文件 + 日历里 30 天内生效的
    upcoming = list(upcoming or [])
    seen_keys = {"".join((it.get("doc_no") or it.get("title") or "").split())[:40] for it in upcoming}
    for e in pending:
        doc = e.get("doc") or {}
        key = "".join((doc.get("doc_no") or doc.get("title_clean") or e.get("title") or "").split())[:40]
        if key in seen_keys:
            continue
        seen_keys.add(key)
        upcoming.append({
            "event_id": e.get("event_id"), "title": doc.get("title_clean") or e.get("title"),
            "doc_no": doc.get("doc_no") or "", "effective_date": doc.get("effective_date") or "",
            "days_left": None,
            "url": e.get("url"),
        })
    for it in upcoming:
        if it.get("days_left") is None:
            eff = _parse(it.get("effective_date"))
            if eff is not None:
                from datetime import date as _d
                it["days_left"] = (eff - _d.today()).days
    upcoming.sort(key=lambda x: (x.get("effective_date") or ""))

    elements: list[dict] = []

    if news:
        elements.append(_section_title(f"🆕 新政速览（{len(news)} 条）"))
        for i, e in enumerate(news[:max_items], 1):
            if i > 1:
                elements.append({"tag": "hr"})
            elements.append(_item_block(i, e))
        if len(news) > max_items:
            elements.append({"tag": "note", "elements": [{
                "tag": "plain_text",
                "content": f"另有 {len(news) - max_items} 条未展开，可到仓库 outbox 查看"}]})

    if upcoming:
        if elements:
            elements.append({"tag": "hr"})
        elements.append(_section_title(f"⏰ 即将生效（{len(upcoming)} 条）"))
        lines = []
        for it in upcoming:
            left = int(it.get("days_left") or 0)
            when = "**今天生效**" if left == 0 else f"**{left} 天后**"
            no = f"{_h(it.get('doc_no'))}　" if it.get("doc_no") else ""
            lines.append(f"• {when}（{it.get('effective_date')}）　{no}"
                         f"[{_h(it.get('title'))[:46]}]({it.get('url')})")
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}})

    if drafts:
        if elements:
            elements.append({"tag": "hr"})
        elements.append(_section_title(f"📝 征求意见中（{len(drafts)} 条，草案）"))
        lines = []
        for e in drafts[:max_items]:
            doc = e.get("doc") or {}
            tail = []
            if doc.get("comment_deadline"):
                tail.append(f"截止 {doc['comment_deadline']}")
            if doc.get("effective_date"):
                tail.append(f"拟 {doc['effective_date']} 施行")
            suffix = ("　" + "／".join(tail)) if tail else ""
            lines.append(f"• [{_h(doc.get('title_clean') or e.get('title'))[:50]}]({e.get('url')}){suffix}")
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}})
        elements.append({"tag": "note", "elements": [{
            "tag": "plain_text", "content": "草案尚未定稿，可按页面指引反馈意见——这类稿子通常 1-3 个月内落地"}]})

    if not elements:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "今天没有值得推送的政策变化。"}})

    risks = [((e.get("doc") or {}).get("risk_level") or "低") for e in news]
    template = "red" if "高" in risks else ("orange" if upcoming else "blue")
    total = len(news) + len(upcoming or []) + len(drafts)
    elements.append({"tag": "note", "elements": [{
        "tag": "plain_text",
        "content": f"共 {total} 条 · 来源：税务总局 / 财政部 / 外汇局　·　未写入思源，需要归档请手动处理",
    }]})

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": f"财税政策日报 · {date_label}"},
        },
        "elements": elements,
    }


def build_and_send(event: dict, dry_run: bool = False) -> bool:
    return send_card(build_card(event), dry_run=dry_run)


def send_digest(events: list[dict], date_label: str = "", dry_run: bool = False,
                upcoming: list[dict] | None = None) -> bool:
    return send_card(build_digest_card(events, date_label, upcoming=upcoming), dry_run=dry_run)


if __name__ == "__main__":
    import sys
    sample = json.load(sys.stdin)
    build_and_send(sample, dry_run="--send" not in sys.argv)
