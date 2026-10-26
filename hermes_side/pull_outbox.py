#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
兜底通道：从 GitHub 仓库的 outbox/ 目录拉取未被接收的公告，补发飞书。

为什么需要它：GitHub Actions 跑在公网，而运行 Hermes 的 NAS 在内网，
Actions 无法直接 POST 到内网地址（除非你做了内网穿透）。watch.py 在
webhook 推送失败时会把事件写进仓库的 outbox/ 目录并提交，这个脚本负责
定时把这些漏掉的补上。两种通道可以同时开着，靠 processed.json 去重。

用法：
    export GITHUB_REPO=Paiyunshou/tax-watch      # owner/repo
    export GITHUB_TOKEN=ghp_xxx                  # 公开仓库可省；私有仓库必填（只需 contents 读权限）
    export FEISHU_WEBHOOK_URL=...
    python3 pull_outbox.py                 # 拉一次，补发
    python3 pull_outbox.py --dry-run       # 只看有哪些没处理
    python3 pull_outbox.py --limit 5
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import requests

import feishu

DATA_DIR = Path(os.environ.get("WATCH_DATA_DIR", Path(__file__).resolve().parent / "data"))
PROCESSED = DATA_DIR / "processed.json"


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


def api_headers() -> dict:
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def list_outbox(repo: str, branch: str) -> list[dict]:
    url = f"https://api.github.com/repos/{repo}/contents/outbox"
    resp = requests.get(url, headers=api_headers(), params={"ref": branch}, timeout=20)
    if resp.status_code == 404:
        print("[pull] 仓库里没有 outbox 目录（说明还没有推送失败的事件）")
        return []
    resp.raise_for_status()
    files = resp.json()
    return [f for f in files if f.get("name", "").endswith(".json")]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--branch", default=os.environ.get("GITHUB_BRANCH", "main"))
    args = ap.parse_args()

    repo = os.environ.get("GITHUB_REPO", "").strip()
    if not repo:
        print("[pull] 请先设置 GITHUB_REPO=owner/repo")
        return 2

    files = list_outbox(repo, args.branch)
    processed = load_processed()
    pending = [f for f in files if Path(f["name"]).stem not in processed][:args.limit]
    print(f"[pull] outbox 共 {len(files)} 个，待处理 {len(pending)} 个")
    if not pending:
        return 0

    sent = 0
    for f in pending:
        event_id = Path(f["name"]).stem
        if args.dry_run:
            print(f"  [dry-run] 待补发 {event_id}  {f['name']}")
            continue
        raw = requests.get(f["download_url"], timeout=20).text
        event = json.loads(raw)
        ok = feishu.build_and_send(event)
        print(f"  {'✓' if ok else '✗'} {event.get('title', '')[:50]}")
        if ok:
            processed.add(event_id)
            sent += 1
    if not args.dry_run:
        save_processed(processed)
    print(f"[pull] 补发完成：{sent} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
