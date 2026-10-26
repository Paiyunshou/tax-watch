#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每天早上跑一次：把 GitHub 仓库 outbox/ 里攒下的新公告合成一张「财税政策日报」发飞书。

跟 pull_outbox.py 的区别：那个是一条一张卡片（实时模式），这个是一条汇总（日报模式）。
两者共用 data/processed.json 去重，不会重复发。

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
from datetime import datetime
from pathlib import Path

import requests

import feishu

API = "https://api.github.com"
DATA_DIR = Path(os.environ.get("WATCH_DATA_DIR", Path(__file__).resolve().parent / "data"))
PROCESSED = DATA_DIR / "processed.json"


def api_headers() -> dict:
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def load_processed() -> set[str]:
    if not PROCESSED.exists():
        return set()
    try:
        return set(json.loads(PROCESSED.read_text(encoding="utf-8")))
    except Exception:  # noqa: BLE001
        return set()


def save_processed(ids: set[str]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PROCESSED.write_text(json.dumps(sorted(ids)[-5000:], ensure_ascii=False), encoding="utf-8")


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


def delete_remote(repo: str, name: str, sha: str, branch: str) -> bool:
    resp = requests.delete(f"{API}/repos/{repo}/contents/outbox/{name}", headers=api_headers(),
                           json={"message": f"chore: 已发送，清理 {name}", "sha": sha,
                                 "branch": branch}, timeout=20)
    return resp.status_code in (200, 201)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--all", action="store_true", help="忽略 processed，全部拉出来预览")
    ap.add_argument("--no-delete", action="store_true", help="发送后不删远端 outbox 文件")
    ap.add_argument("--branch", default=os.environ.get("GITHUB_BRANCH", "main"))
    args = ap.parse_args()

    repo = os.environ.get("GITHUB_REPO", "").strip()
    if not repo:
        print("[daily] 请先设置 GITHUB_REPO=owner/repo")
        return 2

    files = list_outbox(repo, args.branch)
    processed = load_processed()
    pending = files if args.all else [f for f in files if Path(f["name"]).stem not in processed]
    pending = pending[:args.limit]

    if not pending:
        print("[daily] 没有待发的新公告")
        return 0

    events: list[dict] = []
    for f in pending:
        try:
            events.append(fetch_event(repo, f["name"], args.branch))
        except Exception as exc:  # noqa: BLE001
            print(f"[daily] 跳过 {f['name']}：{exc}")

    if not events:
        print("[daily] 全部读取失败，未发送")
        return 1

    label = datetime.now().strftime("%m月%d日")
    ok = feishu.send_digest(events, date_label=label, dry_run=args.dry_run)
    print(f"[daily] {len(events)} 条 → {'预览' if args.dry_run else ('已发送' if ok else '发送失败')}")
    if args.dry_run or not ok:
        return 0 if ok else 1

    sent_ids = set()
    for f in pending:
        eid = Path(f["name"]).stem
        if eid in {e.get("event_id") for e in events}:
            sent_ids.add(eid)
    processed |= sent_ids
    save_processed(processed)

    if not args.no_delete:
        for f in pending:
            if Path(f["name"]).stem in sent_ids:
                delete_remote(repo, f["name"], f["sha"], args.branch)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
