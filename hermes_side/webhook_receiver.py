#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Hermes 侧 Webhook 接收端。

职责很窄：收到 watch.py 推来的 event JSON → 组装飞书卡片 → 发飞书 → 落一份本地日志。
明确不做的事：不写思源笔记、不改任何知识库。有价值的政策由人工确认后再手动归档。

启动：
    export WATCH_TOKEN='和 GitHub Secret 里一致的那串'
    export FEISHU_WEBHOOK_URL='https://open.feishu.cn/open-apis/bot/v2/hook/xxx'
    python3 webhook_receiver.py --port 8010

自测（不启动服务，直接把一个 JSON 文件当请求喂进去）：
    python3 webhook_receiver.py --replay outbox/xxxx.json
    python3 webhook_receiver.py --replay outbox/xxxx.json --dry-run   # 只打印卡片
"""

from __future__ import annotations

import argparse
import json
import os
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import feishu

DATA_DIR = Path(os.environ.get("WATCH_DATA_DIR", Path(__file__).resolve().parent / "data"))
EVENTS_LOG = DATA_DIR / "events.jsonl"
PROCESSED = DATA_DIR / "processed.json"
MAX_BODY = 2 * 1024 * 1024  # 2MB

_lock = threading.Lock()


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def load_processed() -> set[str]:
    if not PROCESSED.exists():
        return set()
    try:
        return set(json.loads(PROCESSED.read_text(encoding="utf-8")))
    except Exception:  # noqa: BLE001
        return set()


def save_processed(ids: set[str]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # 只保留最近 5000 个，防止无限增长
    keep = sorted(ids)[-5000:]
    PROCESSED.write_text(json.dumps(keep, ensure_ascii=False), encoding="utf-8")


def append_log(record: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with EVENTS_LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def handle_event(event: dict, dry_run: bool = False) -> dict:
    """核心处理：幂等校验 → 发飞书 → 记账。返回处理结果。"""
    event_id = str(event.get("event_id") or "").strip()
    if not event_id:
        return {"ok": False, "error": "missing event_id"}

    with _lock:
        processed = load_processed()
        if event_id in processed:
            return {"ok": True, "duplicate": True}

        ok = feishu.build_and_send(event, dry_run=dry_run)
        append_log({
            "received_at": now_iso(),
            "event_id": event_id,
            "source": event.get("source"),
            "title": event.get("title"),
            "risk_level": (event.get("doc") or {}).get("risk_level"),
            "pushed": ok,
            "url": event.get("url"),
        })
        if ok and not dry_run:
            processed.add(event_id)
            save_processed(processed)
        return {"ok": ok, "duplicate": False}


class Handler(BaseHTTPRequestHandler):
    server_version = "tax-watch-receiver/1.0"

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.split("?")[0] in ("/health", "/"):
            self._json(200, {"ok": True, "service": "tax-watch-receiver",
                             "processed": len(load_processed()), "time": now_iso()})
        else:
            self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path.split("?")[0] not in ("/hook", "/", "/webhook"):
            self._json(404, {"ok": False, "error": "not found"})
            return

        expected = os.environ.get("WATCH_TOKEN", "").strip()
        if expected and self.headers.get("X-Watch-Token", "") != expected:
            self._json(401, {"ok": False, "error": "bad token"})
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_BODY:
                raise ValueError(f"bad content-length: {length}")
            event = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(event, dict):
                raise ValueError("payload must be a JSON object")
        except Exception as exc:  # noqa: BLE001
            self._json(400, {"ok": False, "error": f"bad request: {exc}"})
            return

        try:
            result = handle_event(event)
        except Exception as exc:  # noqa: BLE001
            self._json(500, {"ok": False, "error": str(exc)})
            return
        self._json(200 if result.get("ok") else 502, result)

    def log_message(self, fmt: str, *args) -> None:
        print(f"[receiver] {self.address_string()} {fmt % args}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("WATCH_PORT", 8010)))
    ap.add_argument("--host", default=os.environ.get("WATCH_HOST", "0.0.0.0"))
    ap.add_argument("--replay", help="不启服务，直接把一个 event JSON 文件走一遍处理流程")
    ap.add_argument("--dry-run", action="store_true", help="只打印卡片，不发飞书、不记账")
    args = ap.parse_args()

    if args.replay:
        event = json.loads(Path(args.replay).read_text(encoding="utf-8"))
        print(json.dumps(handle_event(event, dry_run=args.dry_run), ensure_ascii=False, indent=2))
        return 0

    print(f"[receiver] 监听 http://{args.host}:{args.port}/hook  "
          f"token={'已配置' if os.environ.get('WATCH_TOKEN') else '未配置（不校验）'}  "
          f"feishu={'已配置' if os.environ.get('FEISHU_WEBHOOK_URL') else '未配置'}")
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
