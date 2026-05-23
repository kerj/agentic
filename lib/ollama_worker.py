#!/usr/bin/env python3
"""
Ollama-based agentic worker with surgical repair loops.

Implements the same tool interface as Claude Code (Read, Edit, Write, Bash, Glob, Grep, LS).
After the main agent loop, runs a build verification and surgical repair cycle:
- One error at a time
- Edit-only in repair mode (no broad rewrites)
- File-locked to only affected files
- Regression detection with automatic revert
- Escalating prompts across 5 strategies
- Context compression via symbol maps when approaching context limit

Usage:
  AGENTIC_HOME=~/.agentic python3 ollama_worker.py "Add dark mode toggle"
  AGENTIC_LOCAL_MODEL=qwen2.5-coder:14b python3 ollama_worker.py "..."
"""

import sys
import os
import json
import re
import subprocess
import glob as glob_module
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional

# ── Configuration ──────────────────────────────────────────────────────────────

OLLAMA_HOST   = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
MODEL         = os.environ.get("AGENTIC_LOCAL_MODEL", "qwen2.5-coder:32b")
AGENTIC_HOME  = Path(os.environ.get("AGENTIC_HOME", Path.home() / ".agentic"))
MAX_TURNS         = int(os.environ.get("AGENTIC_MAX_TURNS", "60"))
MAX_REPAIR_ROUNDS = 5
# Context budget before compression kicks in.
# qwen-coder (128K window) — leave ~28K headroom for output and tool defs.
# Override via AGENTIC_CONTEXT_BUDGET in .agentic.conf
CONTEXT_BUDGET    = int(os.environ.get("AGENTIC_CONTEXT_BUDGET", "100000"))
KEEP_RECENT_TURNS = int(os.environ.get("AGENTIC_KEEP_RECENT_TURNS", "15"))

# ── TypeScript / ESLint error hints ────────────────────────────────────────────

TS_HINTS: dict[str, str] = {
    "TS2749": "refers to a component value used as a type — find the imperative API type (e.g. ListImperativeAPI for react-window's List)",
    "TS2448": "block-scoped variable used before its declaration — move the const/let above where it's used",
    "TS2454": "variable used before being assigned — initialise it before use or guard with undefined check",
    "TS2322": "type mismatch — read the full error, expected vs actual types show exactly what to change",
    "TS2345": "argument type mismatch — check the function signature in the type definitions",
    "TS2307": "module not found — the import path is wrong or the package is not installed",
    "TS2339": "property does not exist on type — check the interface definition, it may need a new property",
    "TS2304": "cannot find name — the symbol is not imported or not defined in scope",
    "TS2366": "function lacks return statement — add a return or change return type to include undefined",
    "TS7006": "parameter implicitly has any type — add a type annotation",
    "TS2531": "object is possibly null — add a null check before accessing the property",
    "TS2532": "object is possibly undefined — add an undefined check before accessing",
    "react-hooks/exhaustive-deps": "add the missing value to the useEffect/useCallback/useMemo dependency array",
    "react-hooks/set-state-in-effect": "move setState out of the effect body into an event handler or callback",
    "react-hooks/rules-of-hooks": "hooks must be called at the top level of a component, not inside conditions or loops",
    "no-unused-vars": "the variable is declared but never used — remove it or use it",
    "import/no-unresolved": "import path cannot be resolved — check the path is correct relative to this file",
}

def enrich_error(error: dict) -> str:
    """Add a semantic hint to a raw error message."""
    code = error.get("code", "")
    msg  = error.get("message", "")
    hint = TS_HINTS.get(code, "")
    location = f"{error['file']} line {error['line']}" if error.get("line") else error.get("file", "")
    base = f"{location}: {code} — {msg}"
    return f"{base}\n  Hint: {hint}" if hint else base

# ── Tool definitions ────────────────────────────────────────────────────────────

def _make_tools(write_enabled: bool = True) -> list[dict]:
    tools = [
        {
            "type": "function",
            "function": {
                "name": "Read",
                "description": "Read the complete contents of a file. Always read a file before editing it.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string", "description": "Path to the file"}
                    },
                    "required": ["file_path"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "Edit",
                "description": "Edit a file by replacing an exact string. old_string must appear exactly once. Use for targeted changes.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file_path":  {"type": "string"},
                        "old_string": {"type": "string", "description": "Exact text to replace (must be unique in file)"},
                        "new_string": {"type": "string", "description": "Replacement text"}
                    },
                    "required": ["file_path", "old_string", "new_string"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "Bash",
                "description": "Run a shell command. Use for git operations, builds, and installs.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command":     {"type": "string"},
                        "description": {"type": "string"},
                        "timeout":     {"type": "integer"}
                    },
                    "required": ["command"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "Glob",
                "description": "Find files matching a glob pattern.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string"},
                        "path":    {"type": "string"}
                    },
                    "required": ["pattern"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "Grep",
                "description": "Search for a pattern in files.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string"},
                        "path":    {"type": "string"},
                        "include": {"type": "string"}
                    },
                    "required": ["pattern"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "LS",
                "description": "List files and directories.",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": []
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "Setup",
                "description": (
                    "Install project dependencies. Call this once at the start of PHASE 1 "
                    "before editing any files. Detects yarn/pnpm/npm from lock files automatically. "
                    "Pass packages=[] to also add new libraries in the same step."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "packages": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Extra packages to install, e.g. ['react-window', '@types/react-window']"
                        }
                    },
                    "required": []
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "Build",
                "description": (
                    "Run the project build and return pass/fail with any errors. "
                    "Reads package.json scripts to find the right build command. "
                    "Use this instead of running npm/yarn/pnpm build via Bash."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            }
        },
    ]
    if write_enabled:
        tools.insert(2, {
            "type": "function",
            "function": {
                "name": "Write",
                "description": "Write complete content to a file. Use for new files or when a full rewrite is cleaner than editing.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string"},
                        "content":   {"type": "string"}
                    },
                    "required": ["file_path", "content"]
                }
            }
        })
    return tools

TOOLS_FULL   = _make_tools(write_enabled=True)
TOOLS_REPAIR = _make_tools(write_enabled=False)   # Edit-only in repair mode

# ── Tool implementations ────────────────────────────────────────────────────────

def tool_read(file_path: str) -> str:
    p = Path(file_path)
    if not p.exists():
        return f"Error: file not found: {file_path}"
    try:
        return p.read_text(errors="replace")
    except Exception as e:
        return f"Error reading {file_path}: {e}"

def tool_edit(file_path: str, old_string: str, new_string: str) -> str:
    p = Path(file_path)
    if not p.exists():
        return f"Error: file not found: {file_path}"
    content = p.read_text(errors="replace")
    count = content.count(old_string)
    if count == 0:
        return f"Error: old_string not found in {file_path}. Use Read to see the current content."
    if count > 1:
        return f"Error: old_string appears {count} times — must be unique. Add more context lines."
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content.replace(old_string, new_string, 1))
    return f"Edited {file_path}"

def tool_write(file_path: str, content: str) -> str:
    p = Path(file_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return f"Wrote {file_path} ({len(content.splitlines())} lines)"

def tool_bash(command: str, description: str = "", timeout: int = 300000) -> str:
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            timeout=min(timeout / 1000, 600)
        )
        out = result.stdout + result.stderr
        if result.returncode != 0:
            return f"Exit {result.returncode}\n{out}"
        return out or "(no output)"
    except subprocess.TimeoutExpired:
        return f"Error: timed out"
    except Exception as e:
        return f"Error: {e}"

def tool_glob(pattern: str, path: str = ".") -> str:
    matches = sorted(str(p) for p in Path(path).glob(pattern) if ".git" not in str(p))
    return "\n".join(matches[:200]) if matches else f"No files matching {pattern}"

def tool_grep(pattern: str, path: str = ".", include: str = "") -> str:
    cmd = ["grep", "-rn", "--include", include or "*", pattern, path]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        out = result.stdout.strip()
        return out[:4000] if out else f"No matches for '{pattern}'"
    except Exception as e:
        return f"Error: {e}"

def tool_ls(path: str = ".") -> str:
    p = Path(path)
    if not p.exists():
        return f"Error: path not found: {path}"
    entries = sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name))
    lines = [
        f"{'  ' if e.is_file() else '📁 '}{e.name}"
        for e in entries[:100]
        if not (e.name.startswith(".") and e.name not in (".gitignore", ".prettierrc", ".eslintrc"))
    ]
    return "\n".join(lines) or "(empty)"

def _detect_package_manager() -> tuple[str, str, str]:
    """Return (name, install_cmd, add_cmd) based on lock files in cwd."""
    cwd = Path.cwd()
    if (cwd / "yarn.lock").exists():
        return "yarn", "yarn install", "yarn add"
    if (cwd / "pnpm-lock.yaml").exists():
        return "pnpm", "pnpm install", "pnpm add"
    return "npm", "npm install", "npm install"

def tool_setup(packages: list | None = None) -> str:
    """
    Install project dependencies and optionally add new packages.
    Detects yarn/pnpm/npm from lock files. Returns clear success or error.
    """
    cwd = Path.cwd()
    if not (cwd / "package.json").exists():
        return "No package.json found — skipping setup (not a Node.js project)."

    pm, install_cmd, add_cmd = _detect_package_manager()

    result = subprocess.run(
        install_cmd, shell=True, capture_output=True, text=True, timeout=300
    )
    if result.returncode != 0:
        out = (result.stdout + result.stderr).strip()
        return f"✗ Setup failed ({install_cmd}):\n{out}"

    extras = []
    if packages:
        pkg_str = " ".join(packages)
        r2 = subprocess.run(
            f"{add_cmd} {pkg_str}", shell=True, capture_output=True, text=True, timeout=120
        )
        if r2.returncode != 0:
            out = (r2.stdout + r2.stderr).strip()
            return f"✗ Failed to add {pkg_str}:\n{out}"
        extras = packages

    msg = f"✓ Setup complete ({pm}). node_modules ready."
    if extras:
        msg += f" Added: {', '.join(extras)}."
    return msg

def _find_build_cmd() -> str:
    """Return the build shell command for the current project, with 2>&1 appended."""
    import json as _json
    cwd = Path.cwd()
    pkg_path = cwd / "package.json"
    if not pkg_path.exists():
        return "npx tsc --noEmit 2>&1"
    try:
        scripts = _json.loads(pkg_path.read_text()).get("scripts", {})
    except Exception:
        scripts = {}
    pm, _, _ = _detect_package_manager()
    runner = pm if pm in ("yarn", "pnpm") else "npm run"
    build_script = next(
        (s for s in ("build", "build:prod", "build:app", "compile") if s in scripts),
        None,
    )
    return f"{runner} {build_script} 2>&1" if build_script else "npx tsc --noEmit 2>&1"


def tool_build() -> str:
    """Run the project build and return pass/fail with any errors."""
    cmd = _find_build_cmd()
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        return "✗ Build timed out after 300s."
    raw = r.stdout or ""
    errors = parse_errors(raw)
    if r.returncode == 0 and not errors:
        return f"✓ Build passed ({cmd.split()[0]})."
    return _format_build_result(errors, raw, cmd)

def _format_build_result(errors: list, raw: str, cmd: str) -> str:
    if errors:
        lines = [f"✗ Build failed — {len(errors)} error(s):"]
        for e in errors[:15]:
            lines.append(f"  {e['file']}:{e.get('line','?')} [{e['code']}] {e['message']}")
        return "\n".join(lines)
    snippet = raw.strip()[-1500:] if raw.strip() else "No output."
    return f"✗ Build failed (bundler/import error):\n{snippet}"


def execute_tool(name: str, args: dict,
                 locked_files: Optional[set] = None) -> tuple[str, bool]:
    """Execute a tool. locked_files blocks Edit/Write on non-error files during repair."""
    try:
        if locked_files and name in ("Edit", "Write"):
            fpath = args.get("file_path", "")
            if fpath and not any(fpath.endswith(lf) or lf.endswith(fpath) for lf in locked_files):
                return (f"Repair mode: only modify files with errors: {sorted(locked_files)}\n"
                        f"'{fpath}' is not in the error list."), True
        if name == "Read":   return tool_read(**args),  False
        if name == "Edit":   return tool_edit(**args),  False
        if name == "Write":  return tool_write(**args), False
        if name == "Bash":
            cmd = args.get("command", "")
            blocked = next((p for p in ("rm -rf", "killall", "pkill", "rmdir /s") if p in cmd), None)
            if blocked:
                return (f"Blocked: '{blocked}' is not allowed. "
                        f"Read the error output and fix the code with Edit instead."), True
            return tool_bash(**args), False
        if name == "Glob":   return tool_glob(**args),  False
        if name == "Grep":   return tool_grep(**args),  False
        if name == "LS":     return tool_ls(**args),    False
        if name == "Setup":  return tool_setup(**args), False
        if name == "Build":
            result = tool_build()
            return result, result.startswith("✗")
        return f"Unknown tool: {name}", True
    except TypeError as e:
        return f"Bad arguments for {name}: {e}", True
    except Exception as e:
        return f"Tool error ({name}): {e}", True

# ── Build verification & error parsing ─────────────────────────────────────────

def run_build() -> tuple[bool, list[dict], str]:
    """Run the project build command. Returns (passed, errors, raw_output)."""
    cmd = _find_build_cmd()
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=False,
            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=300
        )
    except subprocess.TimeoutExpired:
        msg = f"Build timed out after 300s — {cmd.split()[0]} did not finish."
        return False, [{"file": "<build>", "line": 0, "code": "TIMEOUT", "message": msg}], msg
    raw = result.stdout or ""
    errors = parse_errors(raw)
    if result.returncode != 0 and not errors:
        # Vite/bundler error not matching TS/ESLint patterns — pass tail of output to repair
        snippet = raw[-1500:].strip() or "No output captured."
        errors.append({
            "file": "<build>", "line": 0, "code": "BUILD_ERROR",
            "message": f"Build failed (bundler/import error). Output:\n{snippet}"
        })
    passed = result.returncode == 0 and len(errors) == 0
    return passed, errors, raw

def parse_errors(output: str) -> list[dict]:
    """Parse TypeScript compiler and ESLint errors from build output."""
    errors = []

    # TypeScript: src/App.tsx(21,26): error TS2749: message
    for m in re.finditer(
        r"([^\s(]+)\((\d+),\d+\):\s+error\s+(TS\d+):\s+(.+)", output
    ):
        errors.append({
            "file": m.group(1), "line": int(m.group(2)),
            "code": m.group(3), "message": m.group(4).strip()
        })

    # ESLint: /path/file.tsx:84:5: Error: message (rule/name)
    for m in re.finditer(
        r"([^\s:]+\.(?:tsx?|jsx?|js|ts))\s*:(\d+):\d+:\s+Error:\s+(.+?)\s+\(([^)]+)\)", output
    ):
        errors.append({
            "file": m.group(1), "line": int(m.group(2)),
            "code": m.group(4), "message": m.group(3).strip()
        })

    # esbuild/Vite: /path/file.tsx:42:1: error: message
    for m in re.finditer(
        r"([^\s:]+\.(?:tsx?|jsx?)):(\d+):\d+:\s+error:\s+(.+)", output
    ):
        errors.append({
            "file": m.group(1), "line": int(m.group(2)),
            "code": "ESBUILD", "message": m.group(3).strip()
        })

    # Deduplicate by (file, line, code)
    seen, unique = set(), []
    for e in errors:
        key = (e["file"], e.get("line", 0), e["code"])
        if key not in seen:
            seen.add(key)
            unique.append(e)
    return unique

def get_changed_line_count() -> int:
    """Count lines changed since last commit."""
    result = subprocess.run(
        "git diff --shortstat", shell=True, capture_output=True, text=True
    )
    m = re.search(r"(\d+) insertion|(\d+) deletion", result.stdout)
    return int(m.group(1) or m.group(2) or 0) if m else 0

def save_checkpoint() -> str:
    """Stash current state for possible revert. Returns stash ref."""
    r = subprocess.run(
        "git stash push -m 'repair-checkpoint' --include-untracked",
        shell=True, capture_output=True, text=True
    )
    return "stash" if "Saved" in r.stdout else ""

def restore_checkpoint(ref: str) -> None:
    if ref:
        subprocess.run("git stash pop", shell=True, capture_output=True)

# ── Context compression ────────────────────────────────────────────────────────

def estimate_tokens(messages: list) -> int:
    return sum(len(str(m.get("content", ""))) for m in messages) // 4

def compress_ts_to_symbols(content: str) -> str:
    """Reduce TypeScript file to exported interfaces + function signatures only."""
    out = []
    lines = content.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        s = line.strip()
        if re.match(r"^export\s+(interface|type)\s+\w+", s):
            block = [line.rstrip()]
            depth = line.count("{") - line.count("}")
            if depth > 0:
                i += 1
                while i < len(lines) and depth > 0:
                    l = lines[i]
                    block.append(l.rstrip())
                    depth += l.count("{") - l.count("}")
                    i += 1
            out.extend(block)
            out.append("")
            continue
        if re.match(r"^export\s+(?:async\s+)?(?:function|const|class|default|enum|\{)", s):
            out.append(line.rstrip())
            out.append("")
        i += 1
    if out:
        return "\n".join(out).strip() + "\n\n[Compressed — use Read for full content]"
    return f"[{len(lines)} lines, no exports — use Read to see content]"

def compress_css_to_selectors(content: str) -> str:
    selectors = list(dict.fromkeys(re.findall(r"^[.#][\w-]+", content, re.MULTILINE)))
    if not selectors:
        return content if len(content) < 300 else content[:300] + "..."
    return "CSS selectors:\n" + "\n".join(selectors[:50]) + "\n\n[Compressed — use Read for full content]"

def compress_content(content: str, file_path: str) -> str:
    suffix = Path(file_path).suffix
    if suffix in (".ts", ".tsx", ".js", ".jsx"):
        return compress_ts_to_symbols(content)
    if suffix == ".css":
        return compress_css_to_selectors(content)
    lines = content.splitlines()
    if len(lines) <= 15:
        return content
    return "\n".join(lines[:10]) + f"\n... ({len(lines)} lines) [use Read for full content]"

def compress_old_reads(messages: list, keep_recent: int = KEEP_RECENT_TURNS) -> list:
    """
    For Read tool results older than keep_recent messages, replace full file
    content with a compressed symbol map. Keeps the model's working memory
    focused without losing type information.
    """
    cutoff = max(0, len(messages) - keep_recent)
    pending: dict[str, str] = {}   # tool_call_id -> file_path
    compressed = []

    for i, msg in enumerate(messages):
        role = msg.get("role", "")
        if role == "assistant":
            for tc in msg.get("tool_calls", []):
                fn = tc.get("function", {})
                if fn.get("name") == "Read":
                    try:
                        fp = json.loads(fn.get("arguments", "{}")).get("file_path", "")
                        pending[tc["id"]] = fp
                    except Exception:
                        pass
        if (role == "tool" and i < cutoff):
            uid = msg.get("tool_call_id", "")
            fp  = pending.get(uid, "")
            if fp:
                raw = msg.get("content", "")
                msg = {**msg, "content": compress_content(raw, fp)}
        compressed.append(msg)

    return compressed

def maybe_compress(messages: list) -> list:
    if estimate_tokens(messages) > CONTEXT_BUDGET:
        emit({"type": "assistant", "message": {"content": [{
            "type": "text",
            "text": "[Context compressed: old file reads replaced with symbol maps]"
        }]}})
        return compress_old_reads(messages)
    return messages

# ── JSONL event emitter ────────────────────────────────────────────────────────

def emit(event: dict) -> None:
    print(json.dumps(event), flush=True)

# ── Ollama API ─────────────────────────────────────────────────────────────────

def call_ollama(messages: list, model: str,
                tools: list) -> tuple[dict, dict]:
    payload = {
        "model":       model,
        "messages":    messages,
        "tools":       tools,
        "tool_choice": "auto",
        "stream":      False,
        "options":     {"think": False},
    }
    req = urllib.request.Request(
        f"{OLLAMA_HOST}/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            data = json.loads(resp.read())
        return data["choices"][0]["message"], data.get("usage", {})
    except urllib.error.URLError as e:
        print(f"❌ Ollama connection failed: {e}", file=sys.stderr)
        print(f"   ollama serve && ollama pull {model}", file=sys.stderr)
        sys.exit(1)

# ── Agent loop ─────────────────────────────────────────────────────────────────

def run_agent_loop(messages: list, model: str, tools: list,
                   max_turns: int = MAX_TURNS,
                   locked_files: Optional[set] = None) -> tuple[list, int, int]:
    """Run tool-use loop until no more tool calls. Returns (messages, in_tok, out_tok)."""
    total_in = total_out = 0
    consecutive_bash = 0  # reset when a Read/Edit/Write happens

    for _ in range(max_turns):
        messages = maybe_compress(messages)
        msg, usage = call_ollama(messages, model, tools)
        total_in  += usage.get("prompt_tokens", 0)
        total_out += usage.get("completion_tokens", 0)

        if msg.get("content"):
            emit({"type": "assistant", "message": {
                "content": [{"type": "text", "text": msg["content"]}]
            }})

        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            break

        messages.append(msg)

        for tc in tool_calls:
            name = tc["function"]["name"]
            raw_args = tc["function"].get("arguments", "{}")
            try:
                args = json.loads(raw_args)
            except json.JSONDecodeError:
                # Model produced malformed JSON — emit error and let it self-correct
                emit({"type": "tool_use", "id": tc["id"], "name": name, "input": {}})
                emit({"type": "tool_result", "tool_use_id": tc["id"],
                      "content": f"Invalid JSON in tool arguments: {raw_args[:200]}\n"
                                 f"Please provide valid JSON arguments for {name}.",
                      "is_error": True})
                messages.append({
                    "role": "tool", "tool_call_id": tc["id"],
                    "content": f"Malformed arguments. Retry {name} with valid JSON."
                })
                continue

            emit({"type": "tool_use", "id": tc["id"], "name": name, "input": args})

            # Track consecutive Bash calls — spiral detection
            if name == "Bash":
                consecutive_bash += 1
            elif name in ("Read", "Edit", "Write"):
                consecutive_bash = 0

            if consecutive_bash >= 5:
                result = (
                    "SPIRAL DETECTED: You have run 5 Bash commands in a row without "
                    "reading or editing any files. Stop running commands. "
                    "If npm install output was unclear, assume it succeeded and move on. "
                    "Use Read on the relevant source file. Use Edit to make changes. "
                    "Do not run any more Bash commands until you have made an edit."
                )
                is_error = True
                emit({"type": "tool_result", "tool_use_id": tc["id"],
                      "content": result, "is_error": True})
                messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
                consecutive_bash = 0
                continue

            # Detect loop: same call repeated
            recent = [
                m for m in messages[-6:] if m.get("role") == "assistant"
                for t in m.get("tool_calls", [])
                if t["function"]["name"] == name
                and t["function"].get("arguments") == tc["function"].get("arguments")
            ]
            if len(recent) >= 3:
                result   = "You have called this tool 3 times with identical arguments without progress. Stop and try a different approach."
                is_error = True
            else:
                result, is_error = execute_tool(name, args, locked_files)

            emit({"type": "tool_result", "tool_use_id": tc["id"],
                  "content": result, "is_error": is_error})
            messages.append({
                "role": "tool", "tool_call_id": tc["id"], "content": result
            })

    return messages, total_in, total_out

# ── Repair strategies ──────────────────────────────────────────────────────────

REPAIR_STRATEGIES = [
    # Round 0 — metacognitive first (local models need to reason before acting)
    ("Build failed. Before making ANY changes:\n"
     "1. Read each file listed below\n"
     "2. For each error, state the ROOT CAUSE in one sentence — not the symptom\n"
     "3. Describe exactly which string you will change and why that fixes the cause\n"
     "Then use Edit only (Write is disabled) to make those specific changes.\n\n"
     "Files with errors: {files}\n\nErrors:\n{errors}"),

    # Round 1 — single error focus
    ("Still failing. Ignore all other errors. Fix only this one:\n\n"
     "{first_error}\n\n"
     "Read the file. Identify the root cause. Use Edit for the minimal change."),

    # Round 2 — single error focus
    ("Ignore all other errors. Fix only this one error, nothing else:\n\n"
     "{first_error}\n\n"
     "Read the file first. Use Edit to change only the broken line. Stop after."),

    # Round 3 — forced re-read
    ("Stop all editing. Read every file listed below completely, "
     "then fix only the first error.\n\n"
     "Files to read: {files}\n\nFix only: {first_error}"),

    # Round 4 — hard reset
    ("Your edits have not fixed the build after multiple attempts. "
     "Run: git checkout -- {files_space}\n"
     "Then fix only this one error from scratch: {first_error}"),
]

def repair_single_error(error: dict, messages: list, model: str,
                        all_error_files: set, strategy_idx: int) -> list:
    """
    Attempt to fix one error surgically. Returns updated messages.
    Whether the fix succeeded is determined by the caller running the build.
    """
    enriched = enrich_error(error)
    files     = sorted(all_error_files)
    template  = REPAIR_STRATEGIES[min(strategy_idx, len(REPAIR_STRATEGIES) - 1)]

    prompt = template.format(
        files=" ".join(files),
        files_space=" ".join(files),
        errors=enriched,
        first_error=enriched,
    )

    emit({"type": "assistant", "message": {"content": [{
        "type": "text",
        "text": f"\n[Repair strategy {strategy_idx}: targeting {error['file']} line {error.get('line', '?')}]"
    }]}})

    messages = messages + [{"role": "user", "content": prompt}]
    messages, _, _ = run_agent_loop(
        messages, model, TOOLS_REPAIR,
        max_turns=8,
        locked_files=all_error_files,
    )
    return messages

def repair_loop(request: str, messages: list, model: str,
                initial_errors: list) -> tuple[bool, list, int, int]:
    """
    Surgical repair: one error at a time, Edit-only, file-locked.
    Returns (build_passed, messages, total_input_tokens, total_output_tokens).
    """
    total_in = total_out = 0
    errors   = initial_errors
    strategy = 0

    for round_num in range(MAX_REPAIR_ROUNDS):
        if not errors:
            # No errors left — verify the build actually passes before declaring success
            passed_check, _, _ = run_build()
            return passed_check, messages, total_in, total_out

        # Separate real file errors from synthetic bundler errors
        real_errors    = [e for e in errors if not e["file"].startswith("<")]
        bundler_errors = [e for e in errors if e["file"].startswith("<")]
        error_files    = {e["file"] for e in real_errors}  # only real files are locked
        prev_count     = len(errors)

        emit({"type": "assistant", "message": {"content": [{
            "type": "text",
            "text": f"\n── Repair round {round_num + 1}/{MAX_REPAIR_ROUNDS} "
                    f"({len(errors)} error(s), strategy {strategy}) ──"
        }]}})

        # Checkpoint before each repair round
        checkpoint = save_checkpoint()

        if real_errors:
            # Fix structured errors one at a time (file-locked)
            for error in real_errors:
                messages = repair_single_error(error, messages, model, error_files, strategy)
        else:
            # Bundler/import error with no file pointer — let model diagnose freely
            build_msg = "\n\n".join(e["message"] for e in bundler_errors)
            prompt = (
                f"Build failed with no TypeScript errors detected.\n"
                f"This is likely a bundler, import, or export error.\n\n"
                f"{build_msg}\n\n"
                f"Read the relevant source files, find the root cause, and fix it. "
                f"Use Edit only (Write is disabled)."
            )
            messages = messages + [{"role": "user", "content": prompt}]
            messages, _, _ = run_agent_loop(
                messages, model, TOOLS_REPAIR, max_turns=8, locked_files=None
            )

        # Verify
        passed, new_errors, _ = run_build()
        new_count = len(new_errors)

        if passed:
            emit({"type": "assistant", "message": {"content": [{
                "type": "text", "text": "✅ Build passing after repair."
            }]}})
            if checkpoint:
                subprocess.run("git stash drop", shell=True, capture_output=True)
            return True, messages, total_in, total_out

        if new_count > prev_count:
            # Regression — revert and escalate
            emit({"type": "assistant", "message": {"content": [{
                "type": "text",
                "text": f"⚠️  Repair introduced new errors ({prev_count} → {new_count}). Reverting."
            }]}})
            restore_checkpoint(checkpoint)
            strategy = min(strategy + 1, len(REPAIR_STRATEGIES) - 1)
        elif new_count == prev_count:
            # No progress — escalate
            strategy = min(strategy + 1, len(REPAIR_STRATEGIES) - 1)
            if checkpoint:
                subprocess.run("git stash drop", shell=True, capture_output=True)
        else:
            # Progress — continue with same strategy, drop checkpoint
            strategy = max(0, strategy - 1)
            if checkpoint:
                subprocess.run("git stash drop", shell=True, capture_output=True)

        errors = new_errors

    return False, messages, total_in, total_out

# ── Main ───────────────────────────────────────────────────────────────────────

def build_repo_map() -> str:
    """Build a compact symbol index of the project for upfront context."""
    try:
        result = subprocess.run(
            r"""find . -type f \( -name "*.ts" -o -name "*.tsx" -o -name "*.js" -o -name "*.jsx" \) \
                -not -path "*/node_modules/*" -not -path "*/.git/*" \
                -not -path "*/.claude/*" -not -path "*/dist/*" \
                -not -path "*/build/*" | sort | head -100""",
            shell=True, capture_output=True, text=True, timeout=10
        )
        files = [f for f in result.stdout.strip().splitlines() if f]
        if not files:
            return ""

        lines = []
        for fp in files[:60]:          # cap at 60 files
            content = Path(fp).read_text(errors="replace")
            exports = []
            for m in re.finditer(
                r"^(?:export\s+(?:(?:async|default|declare|abstract)\s+)*"
                r"(?:function\*?\s+|const\s+|let\s+|class\s+|interface\s+|type\s+|enum\s+)"
                r"|module\.exports\s*=\s*)"
                r"(\w+)", content, re.MULTILINE
            ):
                exports.append(m.group(1))
            if exports:
                lines.append(f"{fp[2:]}: {', '.join(exports[:8])}")
        return "\n".join(lines)
    except Exception:
        return ""


def run(request: str, model: str, system_prompt: str) -> int:
    # Inject repo map as first user turn so model starts with project overview
    repo_map = build_repo_map()
    worktree = os.getcwd()  # bash commands and file ops run here
    target_repo = os.environ.get("AGENTIC_TARGET_REPO", worktree)
    initial_context = request
    if repo_map:
        initial_context = (
            f"WORKING DIRECTORY: {worktree}\n"
            f"(Isolated worktree for project: {target_repo})\n"
            f"All commands run from this directory. Never use cd.\n\n"
            f"PROJECT SYMBOL MAP (file → exported symbols):\n{repo_map}\n\n"
            f"Use this to understand what already exists before reading files.\n\n"
            f"TASK:\n{request}"
        )
    else:
        initial_context = (
            f"WORKING DIRECTORY: {worktree}\n"
            f"(Isolated worktree for project: {target_repo})\n"
            f"All commands run from this directory. Never use cd.\n\n"
            f"TASK:\n{request}"
        )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user",   "content": initial_context},
    ]

    # Phase 1: Main agent loop
    messages, in1, out1 = run_agent_loop(messages, model, TOOLS_FULL)

    # Phase 2: Build verification
    emit({"type": "assistant", "message": {"content": [{
        "type": "text", "text": "\n── Verifying build ──"
    }]}})
    passed, errors, raw = run_build()

    if passed:
        emit({"type": "assistant", "message": {"content": [{
            "type": "text", "text": "✅ Build passed."
        }]}})
    else:
        emit({"type": "assistant", "message": {"content": [{
            "type": "text",
            "text": f"❌ Build failed ({len(errors)} error(s)). Entering surgical repair loop."
        }]}})
        passed, messages, in2, out2 = repair_loop(request, messages, model, errors)
        in1 += in2; out1 += out2

    # Phase 3: Ensure commit
    status = subprocess.run(
        "git status --porcelain", shell=True, capture_output=True, text=True
    ).stdout.strip()
    if status:
        if passed:
            subprocess.run(
                "git add -A && git commit -m 'agent: apply changes'",
                shell=True, capture_output=True
            )
        # If build failed and uncommitted changes exist, leave for inspection

    emit({"type": "result", "usage": {
        "input_tokens": in1, "output_tokens": out1
    }})

    return 0 if passed else 1


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: ollama_worker.py '<request>'", file=sys.stderr)
        sys.exit(1)

    request     = sys.argv[1]
    prompt_path = os.environ.get("AGENTIC_WORKER_PROMPT", "")
    prompt_file = Path(prompt_path) if prompt_path else AGENTIC_HOME / "agents" / "worker_local.txt"
    if not prompt_file.exists():
        prompt_file = AGENTIC_HOME / "agents" / "worker.txt"
    system_prompt = prompt_file.read_text() if prompt_file.exists() else ""

    sys.exit(run(request, MODEL, system_prompt))
