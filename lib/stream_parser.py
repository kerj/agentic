#!/usr/bin/env python3
"""
Reads a Claude Code / Ollama worker stream-json JSONL stream from stdin
and prints human-readable output — assistant text, tool calls, results.
"""
import sys
import json

_last_tool = None   # track name of most recent tool_use for result labeling

for raw_line in sys.stdin:
    line = raw_line.strip()
    if not line:
        continue
    try:
        ev = json.loads(line)
    except Exception:
        continue

    t = ev.get("type", "")

    # ── Assistant text ────────────────────────────────────────────────────────
    if t == "assistant":
        for block in ev.get("message", {}).get("content", []):
            if block.get("type") == "text":
                print(block["text"], end="", flush=True)

    # ── Tool calls ────────────────────────────────────────────────────────────
    elif t == "tool_use":
        name = ev.get("name", "")
        inp  = ev.get("input", {})
        _last_tool = name

        if name in ("Read", "Edit", "Write"):
            path = inp.get("file_path") or inp.get("path", "")
            print(f"\n[{name}] {path}", flush=True)

        elif name == "Bash":
            cmd = (inp.get("command") or "").strip()[:120]
            print(f"\n[Bash] {cmd}", flush=True)

        elif name == "Glob":
            print(f"\n[Glob] {inp.get('pattern', '')}", flush=True)

        elif name == "Grep":
            print(f"\n[Grep] {inp.get('pattern', '')} in {inp.get('path', '.')}", flush=True)

        elif name == "LS":
            print(f"\n[LS] {inp.get('path', '.')}", flush=True)

        elif name == "Setup":
            pkgs = inp.get("packages") or []
            suffix = f" + {pkgs}" if pkgs else ""
            print(f"\n[Setup] installing dependencies{suffix}…", flush=True)

        elif name == "Build":
            print(f"\n[Build] running…", flush=True)

        elif name == "TileConvert":
            img  = inp.get("image_path", "")
            n    = inp.get("name", "")
            kind = "sprite" if inp.get("sprite") else "tiles"
            print(f"\n[TileConvert] {img} → {n} ({kind})", flush=True)

        elif name == "RomUsage":
            print(f"\n[RomUsage] checking ROM/RAM usage…", flush=True)

        elif name == "Symbols":
            flt = inp.get("filter", "")
            print(f"\n[Symbols]{' filter=' + flt if flt else ''}", flush=True)

        else:
            print(f"\n[{name}]", flush=True)

    # ── Tool results (show for key tools so pass/fail is visible) ─────────────
    elif t == "tool_result":
        content  = str(ev.get("content", ""))
        is_error = ev.get("is_error", False)
        if _last_tool in ("Build", "TileConvert", "RomUsage", "Symbols", "Setup"):
            lines = [l for l in content.splitlines() if l.strip()]
            marker = "  ✗" if is_error else "  ✓"
            # Show first meaningful line + ROM usage summary if present
            if lines:
                print(f"{marker} {lines[0][:120]}", flush=True)
            # For RomUsage show all lines (it's the whole report)
            if _last_tool == "RomUsage" and not is_error:
                for l in lines[1:]:
                    print(f"     {l}", flush=True)
        _last_tool = None

    # ── Live progress (running tokens + context size) ─────────────────────────
    # Emitted on a sentinel line (leading SOH) so the dashboard can route it to a
    # live header counter instead of the log body. A human reading the terminal
    # just sees a compact one-liner.
    elif t == "progress":
        tok = ev.get("tokens", {})
        ctx = ev.get("ctx", {})
        payload = json.dumps({
            "input":  tok.get("input", 0),
            "output": tok.get("output", 0),
            "ctx_used":   ctx.get("used", 0),
            "ctx_budget": ctx.get("budget", 0),
        })
        # Leading newline guarantees the sentinel is its own line — assistant text
        # is printed with end="" so without it the sentinel glues onto prior text.
        print(f"\n\x01PROGRESS {payload}", flush=True)

    # ── Final usage summary ───────────────────────────────────────────────────
    elif t == "result":
        usage = ev.get("usage", {})
        inp   = usage.get("input_tokens", 0)
        out   = usage.get("output_tokens", 0)
        if inp or out:
            print(f"\n📊 Tokens: {inp:,} in + {out:,} out = {inp+out:,} total", flush=True)
