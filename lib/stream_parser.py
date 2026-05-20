#!/usr/bin/env python3
"""
Reads a Claude Code stream-json JSONL stream from stdin and prints
human-readable output — assistant text, file operations, commands run.
"""
import sys
import json

for raw_line in sys.stdin:
    line = raw_line.strip()
    if not line:
        continue
    try:
        ev = json.loads(line)
    except Exception:
        continue

    t = ev.get("type", "")

    if t == "assistant":
        for block in ev.get("message", {}).get("content", []):
            if block.get("type") == "text":
                print(block["text"], end="", flush=True)

    elif t == "tool_use":
        name = ev.get("name", "")
        inp  = ev.get("input", {})
        if name in ("Read", "Edit", "Write"):
            path = inp.get("file_path") or inp.get("path", "")
            print(f"\n[{name}] {path}", flush=True)
        elif name == "Bash":
            cmd = (inp.get("command") or "").strip()[:120]
            print(f"\n[Bash] {cmd}", flush=True)

    elif t == "result":
        usage = ev.get("usage", {})
        inp   = usage.get("input_tokens", 0)
        out   = usage.get("output_tokens", 0)
        if inp or out:
            print(f"\n📊 Tokens: {inp:,} in + {out:,} out = {inp+out:,} total", flush=True)
