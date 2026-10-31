#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每天早上跑一次：把 GitHub 仓库 outbox/ 里的新公告 + calendar.json 里的「即将生效」
合成一张「财税政策日报」发飞书。

日报分三块，正好对应三类真正有价值的信息：
  ① 新政速览   —— 最近一个月内新出台的（按风险排序）
  ② 即将生效   —— 早已发布、但生效日临近的（30 天窗口内，按 30/14/7/3/1/0 天提醒）
  ③ 征求意见中 —— 还在征集意见的草案，即「快要出台的新政」

跟 pull_outbox.py 的区别：那个是一条一张卡片（实时模式），这个是一条汇总（日报模式）。
两者共用 data/processed.json 去重，不会重复发。
「即将生效」的提醒进度记在 data/calendar_notified.json，同一天数不会重复提醒。

发送成功后顺手删掉仓库里的那个 outbox 文件，避免仓库无限膨胀。

用法：
    python3 pull_daily.py --dry-run --all     # 预览今天会发成什么样（不发送）
    python3 pull_daily.py                     # 真正发送
"""

from __future__ import annotations

import argparse
import base64
import json
import os
from datetime import date, datetime
from pathlib import Path

import requests

import feishu

API = "https://api.github.com"
DATA_DIR = Path(os.environ.get("WATCH_DATA_DIR", Path(__file__).resolve().parent / "data"))
PROCESSED = DATA_DIR / "processed.json"
NOTIFIED = DATA_DIR / "calendar_notified.json"

# 生效倒计时提醒节点：进入 30 天窗口先提醒一次，之后在这些天数上再提醒
UPCOMING_WINDOW = 30
REMIND_DAYS = {14, 7, 3, 1, 0}


def api_headers() -> dict:
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def load_json_set(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        return set(json.loads(path.read_text(encoding="utf-8")))
    except Exception:  # noqa: BLE001
        return set()


def save_json_set(path: Path, ids: set[str]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(ids), ensure_ascii=False), encoding="utf-8")


def load_processed() -> set[str]:
    return load_json_set(PROCESSED)


def save_processed(ids: set[str]) -> None:
    save_json_set(PROCESSED, ids)


def list_outbox(repo: str, branch: str) -> list[dict]:
    resp = requests.get(f"{API}/repos/{repo}/contents/outbox", headers=api_headers(),
                        params={"ref": branch}, timeout=20)
    if resp.status_code == 404:
        return []
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list):
        return []
    return [f for f in data if f.get("name", "").endswith(".json")]


def fetch_event(repo: str, name: str, branch: str) -> dict:
    resp = requests.get(f"{API}/repos/{repo}/contents/outbox/{name}", headers=api_headers(),
                        params={"ref": branch}, timeout=20)
    resp.raise_for_status()
    content = resp.json().get("content") or ""
    if not content:
        raise RuntimeError("文件过大或为空")
    return json.loads(base64.b64decode(content).decode("utf-8"))


def fetch_calendar(repo: str, branch: str) -> list[dict]:
    """仓库根目录的 calendar.json：还没生效的政策条目。"""
    resp = requests.get(f"{API}/repos/{repo}/contents/calendar.json", headers=api_headers(),
                        params={"ref": branch}, timeout=20)
    if resp.status_code == 404:
        return []
    try:
        resp.raise_for_status()
        content = resp.json().get("content") or ""
        data = json.loads(base64.b64decode(content).decode("utf-8"))
        return list((data.get("items") or {}).values())
    except Exception as exc:  # noqa: BLE001
        print(f"[daily] calendar.json 读取失败：{exc}")
        return []


def delete_remote(repo: str, name: str, sha: str, branch: str) -> bool:
    resp = requests.delete(f"{API}/repos/{repo}/contents/outbox/{name}", headers=api_headers(),
                           json={"message": f"chore: 已发送，清理 {name}", "sha": sha,
                                 "branch": branch}, timeout=20)
    return resp.status_code in (200, 201)


def parse_date(value: str) -> date | None:
    try:
        return datetime.strptime((value or "")[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def pick_upcoming(items: list[dict], notified: set[str], force: bool = False) -> list[dict]:
    """未来 30 天内生效的条目；按提醒节点筛掉重复提醒过的。"""
    today = date.today()
    out: list[dict] = []
    for it in items:
        eff = parse_date(it.get("effective_date", ""))
        if eff is None:
            continue
        left = (eff - today).days
        if not (0 <= left <= UPCOMING_WINDOW):
            continue
        key = f"{it.get('event_id')}:{left}"
        if not force and key in notified:
            continue
        entry = dict(it)
        entry["days_left"] = left
        out.append(entry)
        notified.add(key)
    out.sort(key=lambda x: x["days_left"])
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--all", action="store_true", help="忽略 processed，全部拉出来预览")
    ap.add_argument("--no-delete", action="store_true", help="发送后不删远端 outbox 文件")
    ap.add_argument("--branch", default=os.environ.get("GITHUB_BRANCH", "main"))
    ap.add_argument("--no-upcoming", action="store_true", help="本次不发「即将生效」倒计时")
    args = ap.parse_args()

    repo = os.environ.get("GITHUB_REPO", "").strip()
    if not repo:
        print("[daily] 请先设置 GITHUB_REPO=owner/repo")
        return 2

    files = list_outbox(repo, args.branch)
    processed = load_processed()
    pending = files if args.all else [f for f in files if Path(f["name"]).stem not in processed]
    pending = pending[:args.limit]

    events: list[dict] = []
    for f in pending:
        try:
            events.append(fetch_event(repo, f["name"], args.branch))
        except Exception as exc:  # noqa: BLE001
            print(f"[daily] 跳过 {f['name']}：{exc}")

    notified = load_json_set(NOTIFIED)
    upcoming = [] if args.no_upcoming else pick_upcoming(
        fetch_calendar(repo, args.branch), notified, force=args.dry_run or args.all)

    if not events and not upcoming:
        print("[daily] 今天没有新公告，也没有临近生效的政策，不发")
        return 0

    label = datetime.now().strftime("%m月%d日")
    ok = feishu.send_digest(events, date_label=label, upcoming=upcoming, dry_run=args.dry_run)
    print(f"[daily] 新政 {len(events)} 条 + 即将生效 {len(upcoming)} 条 → "
          f"{'预览' if args.dry_run else ('已发送' if ok else '发送失败')}")
    if args.dry_run or not ok:
        return 0 if ok else 1

    all_ids = {e.get("event_id") for e in events}
    sent_ids = {Path(f["name"]).stem for f in pending if Path(f["name"]).stem in all_ids}
    processed |= sent_ids
    save_processed(processed)
    save_json_set(NOTIFIED, notified)

    if not args.no_delete:
        for f in pending:
            if Path(f["name"]).stem in sent_ids:
                delete_remote(repo, f["name"], f["sha"], args.branch)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
