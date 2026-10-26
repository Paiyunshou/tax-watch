#!/usr/bin/env python3
"""把 seen.json 概况写进 GitHub Actions 的 Job Summary，方便一眼看运行结果。"""
import json
import os
import pathlib

p = pathlib.Path("seen.json")
lines = ["## 本次运行", ""]
if p.exists():
    data = json.loads(p.read_text(encoding="utf-8"))
    rows = [("源", "已记录条目", "最近成功时间")]
    for name, bucket in (data.get("sources") or {}).items():
        rows.append((name, str(len(bucket.get("urls") or {})), bucket.get("last_ok", "")))
    lines += ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * len(rows[0])]
    lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
else:
    lines.append("seen.json 尚未生成（首次运行或全部源失败）")

out = os.environ.get("GITHUB_STEP_SUMMARY")
if out:
    with open(out, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
else:
    print("\n".join(lines))
