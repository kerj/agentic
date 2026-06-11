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
  AGENTIC_LOCAL_MODEL=qwen-coder:latest python3 ollama_worker.py "..."
"""

import sys
import os
import json
import re
import shutil
import subprocess
import glob as glob_module
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional

from diff_guard import (
    NET_BINS, DESTRUCTIVE_BINS, command_risk, scan_diff, repair_cheat_reason,
)

# ── Configuration ──────────────────────────────────────────────────────────────

OLLAMA_HOST   = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
AGENTIC_HOME  = Path(os.environ.get("AGENTIC_HOME", Path.home() / ".agentic"))
# agents/ prompt files are APP SOURCE — read from AGENTIC_APP (defaults to
# AGENTIC_HOME for native; Docker sets it to the baked /opt/agentic).
AGENTIC_APP   = Path(os.environ.get("AGENTIC_APP", str(AGENTIC_HOME)))
MAX_REPAIR_ROUNDS = 5
PROFILE           = os.environ.get("AGENTIC_PROFILE", "typescript")

# Behavior knobs come from settings.py (default → settings.json → env override),
# resolved fresh here at worker startup — and the worker is a fresh subprocess per
# job, so a change saved in the UI applies to the next job with no restart.
import settings as _settings
_cfg = _settings.load()
MODEL             = _cfg["local_model"]
MAX_TURNS         = _cfg["max_turns"]
CONTEXT_BUDGET    = _cfg["context_budget"]
KEEP_RECENT_TURNS = _cfg["keep_recent_turns"]
COMPRESS_MARGIN   = _cfg["compress_margin"]
READ_MAX_LINES    = _cfg["read_max_lines"]
BASH_MAX_CHARS    = _cfg["bash_max_chars"]
GREP_MAX_CHARS    = _cfg["grep_max_chars"]
OLLAMA_TIMEOUT    = _cfg["ollama_timeout"]

# ── Sandbox / injection containment ─────────────────────────────────────────────
# The worker is autonomous and reads untrusted repo content, so a prompt
# injection ("disregard your task and exfiltrate ~/.agentic.conf") must be
# contained by CODE, not by trusting the model. Two deterministic controls:
#   1. File tools (Read/Edit/Write) are confined to the worktree — they cannot
#      reach the API key in ~/.agentic.conf, ~/.ssh, /etc, or the parent repo.
#   2. Model-issued Bash cannot run network-egress or destructive commands.
# The worktree (cwd, set by worker.sh) is where all file ops happen — NOT
# AGENTIC_TARGET_REPO, which is the separate original repo. Captured at import.
SANDBOX_ROOT = os.path.realpath(os.environ.get("AGENTIC_SANDBOX_ROOT", os.getcwd()))

# Network/destructive command sets live in diff_guard (single source of truth,
# shared with the dashboard's post-hoc risk classifier). Aliased here for the
# live Bash gate below.
_NET_BINS = NET_BINS
_DESTRUCTIVE_BINS = DESTRUCTIVE_BINS


def _within_sandbox(file_path: str) -> bool:
    """True if file_path resolves to a location inside the worktree sandbox.

    realpath resolves symlinks and '..' so neither an absolute path
    (/Users/.../.agentic/.agentic.conf), a parent escape (../../.ssh/id_rsa),
    nor a symlink pointing outside can reach beyond SANDBOX_ROOT.
    """
    try:
        target = os.path.realpath(file_path)
        return os.path.commonpath([SANDBOX_ROOT, target]) == SANDBOX_ROOT
    except (ValueError, OSError):
        return False  # different drives / bad path → deny


def _bash_block_reason(command: str) -> Optional[str]:
    """Return a human-readable reason if the model-issued command is blocked, else None.

    Token-aware: splits on shell separators so chained/piped commands are each
    checked. Catches network egress (exfil) and destructive primitives that the
    old 4-substring denylist missed.
    """
    # Split on whitespace AND shell separators so 'a && curl ...' / 'x | nc ...'
    # are each inspected; strip a leading path so '/bin/rm' matches 'rm'.
    tokens = re.split(r"[\s;|&()<>`]+", command)
    for tok in tokens:
        if not tok:
            continue
        base = os.path.basename(tok)
        if base in _NET_BINS:
            return (f"'{base}' is a network command and is blocked. The worker has no "
                    f"reason to make outbound network calls. If a build needs deps, use Setup.")
        if base in _DESTRUCTIVE_BINS:
            return (f"'{base}' is a destructive command and is blocked. "
                    f"Fix the code with Edit instead of deleting files.")
    # Catch redirection-to-device and the /dev/tcp reverse-shell trick, which
    # tokenization above would otherwise pass through as a path.
    if re.search(r"/dev/(tcp|udp)/", command):
        return "Network redirection via /dev/tcp is blocked."
    # find/git can destroy the tree without invoking 'rm' as a token.
    if re.search(r"\bfind\b.*\B-(delete|exec)\b", command):
        return "'find -delete'/'find -exec' is blocked. Use Edit to change code, not bulk deletion."
    if re.search(r"\bgit\s+clean\b.*-\w*[fdx]", command):
        return "'git clean' can wipe the worktree and is blocked."
    return None

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

# ── Game Boy C / SDCC error hints ──────────────────────────────────────────────

GB_HINTS: dict[str, str] = {
    "undeclared": "the identifier is not declared — check spelling, include the right header, or declare the variable before use",
    "implicit": "function called before declaration — add a forward declaration or move the function above its caller",
    "incompatible": "type mismatch — GBDK uses UINT8/INT8/UINT16/INT16; check the function signature in the header",
    "too many": "too many arguments to function — check the GBDK header for the correct parameter count",
    "too few": "too few arguments to function — check the GBDK header for the correct parameter count",
    "lvalue": "cannot assign to this expression — you may be assigning to a register address incorrectly",
    "syntax": "syntax error — check for missing semicolons, mismatched braces, or bad macro expansion",
    "redefined": "symbol redefined — declared twice; remove the duplicate or guard with #ifndef",
    "malloc": "malloc is not available on Game Boy — use static or stack allocation instead",
}

def enrich_error(error: dict) -> str:
    """Add a semantic hint to a raw error message."""
    code = error.get("code", "")
    msg  = error.get("message", "").lower()
    hints = GB_HINTS if PROFILE == "gameboy-c" else TS_HINTS
    if PROFILE == "gameboy-c":
        hint = next((v for k, v in hints.items() if k in msg), "")
    else:
        hint = hints.get(code, "")
    location = f"{error['file']} line {error['line']}" if error.get("line") else error.get("file", "")
    base = f"{location}: {code} — {error.get('message', '')}"
    return f"{base}\n  Hint: {hint}" if hint else base

# ── Tool definitions ────────────────────────────────────────────────────────────

def _make_tools(write_enabled: bool = True) -> list[dict]:
    tools = [
        {
            "type": "function",
            "function": {
                "name": "Read",
                "description": ("Read a file's contents. Always read a file before editing it. "
                                "Large files are truncated with a marker; if you see "
                                "'... [N more lines ...]', call Read again with offset to "
                                "continue, or offset+limit to read a specific range "
                                "(e.g. around an error line)."),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string", "description": "Path to the file"},
                        "offset": {"type": "integer", "description": "1-based line to start at (optional)"},
                        "limit": {"type": "integer", "description": "Max lines to return from offset (optional)"}
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
                    "Detects the right build command for this project automatically. "
                    "Use this instead of running build commands via Bash."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            }
        },
    ]

    if PROFILE == "gameboy-c":
        tools += [
            {
                "type": "function",
                "function": {
                    "name": "TileConvert",
                    "description": (
                        "Convert a PNG image to a GBDK C tile/sprite array using png2asset. "
                        "Outputs a .c and .h file in assets/. Use this whenever you need to "
                        "include graphics in the ROM — never write tile data by hand."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "image_path": {
                                "type": "string",
                                "description": "Path to the source PNG file"
                            },
                            "name": {
                                "type": "string",
                                "description": "C identifier name for the generated array (e.g. 'player_sprite')"
                            },
                            "sprite": {
                                "type": "boolean",
                                "description": "True for sprite data, False (default) for background tile data"
                            }
                        },
                        "required": ["image_path", "name"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "RomUsage",
                    "description": (
                        "Parse the build .map file and return ROM and RAM usage per bank. "
                        "Use this after Build() to verify the ROM fits within hardware limits "
                        "(Bank 0: 16KB, each additional bank: 16KB, WRAM: 8KB)."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "required": []
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "Symbols",
                    "description": (
                        "Read the build .sym file and return all symbol addresses grouped by bank. "
                        "Use this to debug linker errors, verify a function was linked, or check "
                        "which bank a symbol ended up in."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "filter": {
                                "type": "string",
                                "description": "Optional substring to filter symbol names (e.g. 'sprite', 'main')"
                            }
                        },
                        "required": []
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "VramAudit",
                    "description": (
                        "Detect VRAM tile index conflicts between background and sprite tile loads. "
                        "Uses cpp to resolve all #define constants, then scans set_bkg_data() and "
                        "set_sprite_data() calls for overlapping index ranges. "
                        "Call this after adding or changing any tile data loads to catch conflicts "
                        "before they corrupt graphics at runtime."
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

# ── Output caps ────────────────────────────────────────────────────────────────
# A small local model has a scarce window; one big tool result can shove its
# working memory out. Cap here in code (deterministic) and ALWAYS leave a visible
# marker so the model learns the output was cut and how to fetch the rest.

def _cap_chars(text: str, limit: int, refine_hint: str) -> str:
    """Keep head + tail around a budget, with a marker naming how much was dropped."""
    if len(text) <= limit:
        return text
    head = limit * 3 // 4
    tail = limit - head
    dropped = len(text) - limit
    return (
        text[:head]
        + f"\n\n... [{dropped} chars omitted — {refine_hint}] ...\n\n"
        + text[-tail:]
    )

def tool_read(file_path: str, offset: int = 0, limit: int = 0) -> str:
    if not _within_sandbox(file_path):
        return f"Error: '{file_path}' is outside the working directory. You can only read files in this project."
    p = Path(file_path)
    if not p.exists():
        return f"Error: file not found: {file_path}"
    try:
        lines = p.read_text(errors="replace").splitlines()
    except Exception as e:
        return f"Error reading {file_path}: {e}"
    total = len(lines)

    # Output is NOT line-numbered: the model copies text verbatim into Edit's
    # old_string, and a "<n>\t" prefix would never match the real file. Line
    # positions are surfaced only in the truncation markers, where they guide a
    # follow-up range read without contaminating copyable content.

    # Explicit line-range read (offset is 1-based; 0 means "from the start").
    if offset or limit:
        start = max(offset - 1, 0) if offset else 0
        end = start + limit if limit else total
        window = lines[start:end]
        shown_to = start + len(window)
        header = f"[lines {start + 1}-{shown_to} of {total}]\n"
        suffix = "" if shown_to >= total else f"\n... [{total - shown_to} more lines — Read with offset={shown_to + 1}]"
        return header + "\n".join(window) + suffix

    # Whole-file read: keep small files verbatim; head-truncate large ones with a marker.
    if total <= READ_MAX_LINES:
        return "\n".join(lines)
    return (
        "\n".join(lines[:READ_MAX_LINES])
        + f"\n... [{total - READ_MAX_LINES} more lines of {total} — Read with offset={READ_MAX_LINES + 1} "
          f"to continue, or offset=<line> limit=<n> for a specific range]"
    )

def tool_edit(file_path: str, old_string: str, new_string: str) -> str:
    if not _within_sandbox(file_path):
        return f"Error: '{file_path}' is outside the working directory. You can only edit files in this project."
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
    if not _within_sandbox(file_path):
        return f"Error: '{file_path}' is outside the working directory. You can only write files in this project."
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
        # Label streams so the model can tell exit-0 warnings from real failures,
        # and cap with a tail-preserving marker (errors/exit codes land at the end).
        parts = []
        if result.stdout:
            parts.append(result.stdout if result.returncode == 0 else "STDOUT:\n" + result.stdout)
        if result.stderr:
            parts.append(("STDERR:\n" if result.returncode == 0 else "STDERR:\n") + result.stderr)
        out = "\n".join(parts).strip()
        out = _cap_chars(out, BASH_MAX_CHARS, "run a narrower command or read the relevant file")
        if result.returncode != 0:
            return f"Exit {result.returncode}\n{out}"
        return out or "(no output)"
    except subprocess.TimeoutExpired:
        return f"Error: timed out"
    except Exception as e:
        return f"Error: {e}"

def tool_glob(pattern: str, path: str = ".") -> str:
    matches = sorted(str(p) for p in Path(path).glob(pattern) if ".git" not in str(p))
    if not matches:
        return f"No files matching {pattern}"
    shown = matches[:200]
    suffix = "" if len(matches) <= 200 else f"\n... [{len(matches) - 200} more matches — narrow the pattern]"
    return "\n".join(shown) + suffix

def tool_grep(pattern: str, path: str = ".", include: str = "") -> str:
    cmd = ["grep", "-rn", "--include", include or "*", pattern, path]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        out = result.stdout.strip()
        if not out:
            return f"No matches for '{pattern}'"
        # Was silently truncated before — now mark it so the model refines instead
        # of assuming it saw every hit.
        return _cap_chars(out, GREP_MAX_CHARS,
                          f"narrow with include=\"*.ext\" or a more specific pattern")
    except Exception as e:
        return f"Error: {e}"

def tool_ls(path: str = ".") -> str:
    p = Path(path)
    if not p.exists():
        return f"Error: path not found: {path}"
    entries = sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name))
    visible = [
        e for e in entries
        if not (e.name.startswith(".") and e.name not in (".gitignore", ".prettierrc", ".eslintrc"))
    ]
    lines = [f"{'  ' if e.is_file() else '📁 '}{e.name}" for e in visible[:100]]
    if len(visible) > 100:
        lines.append(f"... [{len(visible) - 100} more entries — use Glob with a pattern]")
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

def _read_node_pin() -> Optional[str]:
    """Return the Node version a project pins, or None. Checks (in order):
    .nvmrc, .node-version, then package.json engines.node. Opt-in by design —
    no pin means use the container's default Node."""
    cwd = Path.cwd()
    for fname in (".nvmrc", ".node-version"):
        f = cwd / fname
        if f.exists():
            v = f.read_text().strip().lstrip("v")
            if v:
                return v
    pkg = cwd / "package.json"
    if pkg.exists():
        try:
            eng = json.loads(pkg.read_text()).get("engines", {}).get("node", "")
            # engines often uses ranges (">=18", "^20"); take the first number run.
            m = re.search(r"(\d+(?:\.\d+){0,2})", eng or "")
            if m:
                return m.group(1)
        except Exception:
            pass
    return None

def ensure_node_version() -> Optional[str]:
    """If the project pins a Node version AND fnm is available, install+activate
    it for this worker's subprocesses by putting its bin on PATH. Default path
    (no pin, or no fnm — e.g. native installs) is a no-op: the ambient/default
    Node is used. Returns a status string if it switched, else None."""
    pin = _read_node_pin()
    if not pin:
        return None  # opt-in: no pin → default Node
    if not shutil.which("fnm"):
        return None  # native/no-fnm → can't switch; use ambient Node
    try:
        # Install the pinned version if missing (idempotent), then resolve its bin.
        subprocess.run(["fnm", "install", pin], capture_output=True, text=True, timeout=600)
        r = subprocess.run(["fnm", "exec", f"--using={pin}", "which", "node"],
                           capture_output=True, text=True, timeout=60)
        node_path = r.stdout.strip()
        if r.returncode != 0 or not node_path:
            return f"⚠️  Project pins Node {pin} but fnm could not provide it; using default Node."
        bindir = str(Path(node_path).parent)
        # Prepend the pinned version's bin so node/npm/tsc resolve to it.
        os.environ["PATH"] = bindir + os.pathsep + os.environ.get("PATH", "")
        return f"⬢ Using Node {pin} for this project (fnm)."
    except Exception as exc:
        return f"⚠️  Node {pin} activation failed ({exc}); using default Node."

def ensure_dependencies() -> Optional[str]:
    """Install project deps if missing, BEFORE the baseline build.

    A git worktree is a fresh checkout with NO node_modules (it's gitignored, not
    copied). Without this, the baseline build fails spuriously with 'tsc: not
    found' — which makes the 'no regression vs baseline' gate compare two broken
    builds and silently lose its ability to catch real regressions.

    Idempotent: only runs an install when package.json exists AND deps are
    actually missing, so repos that already have node_modules pay nothing.
    Returns a short status string if it ran (for logging), else None.
    """
    cwd = Path.cwd()
    if not (cwd / "package.json").exists():
        return None  # not a Node project — nothing to install
    # Deps present if node_modules has a populated .bin (tsc/vite live there).
    if (cwd / "node_modules" / ".bin").exists():
        return None  # already installed — skip (fast path)

    pm, _, _ = _detect_package_manager()
    # FROZEN install: never modify the lockfile. We install deps only to make the
    # baseline build real — the lockfile must NOT change, or its platform-specific
    # churn (e.g. linux libc entries vs the repo's macOS lockfile) gets swept into
    # the job's squash commit, polluting the diff and breaking 'Review in IDE'.
    # `npm ci` / `--frozen-lockfile` install exactly what the lockfile pins and
    # leave it untouched. Fall back to a regular install only if no lockfile (or
    # it's out of sync) makes the frozen command fail.
    if pm == "npm":
        frozen, fallback = "npm ci", "npm install --no-save"
    elif pm == "yarn":
        frozen, fallback = "yarn install --frozen-lockfile", "yarn install"
    else:  # pnpm
        frozen, fallback = "pnpm install --frozen-lockfile", "pnpm install --no-save"

    # Belt-and-suspenders: ensure a WRITABLE npm cache even if HOME is somehow
    # unset/unwritable (e.g. a misconfigured container). Prefer the env, else put
    # it under AGENTIC_HOME (always writable — it's the state mount).
    env = dict(os.environ)
    if not env.get("npm_config_cache"):
        cache_base = env.get("HOME") or os.environ.get("AGENTIC_HOME", str(Path.home()))
        env["npm_config_cache"] = str(Path(cache_base) / ".npm")

    def _run(cmd: str):
        try:
            return subprocess.run(cmd, shell=True, capture_output=True, text=True,
                                  timeout=600, env=env)
        except subprocess.TimeoutExpired:
            return None

    result = _run(frozen)
    used = frozen
    if result is None:
        return f"⚠️  Dependency install timed out ({frozen}) — baseline build may be unreliable."
    if result.returncode != 0:
        # Frozen failed (no lockfile / out of sync). Fall back so the build still
        # works; this MAY touch the lockfile, but we restore it below.
        result = _run(fallback)
        used = fallback
        if result is None:
            return f"⚠️  Dependency install timed out ({fallback}) — baseline build may be unreliable."
    if result.returncode != 0:
        tail = (result.stdout + result.stderr).strip()[-500:]
        return f"⚠️  Dependency install failed ({used}); baseline build may be unreliable:\n{tail}"

    # Belt-and-suspenders: if anything still left the lockfile dirty (e.g. the
    # fallback ran), restore it so it never enters the job's diff. Only touches
    # tracked lockfiles that git sees as modified.
    restored = []
    for lock in ("package-lock.json", "yarn.lock", "pnpm-lock.yaml"):
        if (cwd / lock).exists():
            chk = subprocess.run(["git", "diff", "--quiet", "--", lock],
                                 capture_output=True)
            if chk.returncode != 0:  # lockfile is dirty
                subprocess.run(["git", "checkout", "--", lock], capture_output=True)
                restored.append(lock)
    note = f" (restored {', '.join(restored)})" if restored else ""
    return f"📦 Installed dependencies ({used}) before baseline build{note}."

def _find_build_cmd() -> str:
    """Return the build shell command for the current project, with 2>&1 appended."""
    import json as _json
    cwd = Path.cwd()

    # Game Boy C: always use make
    if PROFILE == "gameboy-c":
        return "make 2>&1"

    # Node.js projects
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

def tool_tile_convert(image_path: str, name: str, sprite: bool = False) -> str:
    """Convert a PNG to GBDK tile/sprite C arrays using png2asset."""
    gbdk_home = os.environ.get("GBDK_HOME", str(Path.home() / "gbdk"))
    png2asset = Path(gbdk_home) / "bin" / "png2asset"
    if not png2asset.exists():
        return f"✗ png2asset not found at {png2asset} — check GBDK_HOME."
    if not Path(image_path).exists():
        return f"✗ Image not found: {image_path}"

    # Auto-pad to 8-pixel boundary — png2asset requires dimensions that are multiples of 8
    padded_note = ""
    try:
        from PIL import Image as _Image
        img = _Image.open(image_path).convert("RGBA")
        w, h = img.size
        new_w = (w + 7) & ~7
        new_h = (h + 7) & ~7
        if new_w != w or new_h != h:
            padded = _Image.new("RGBA", (new_w, new_h), (0, 0, 0, 0))
            padded.paste(img, (0, 0))
            tmp_path = image_path + "_padded.png"
            padded.save(tmp_path)
            image_path = tmp_path
            padded_note = f"  (auto-padded {w}×{h} → {new_w}×{new_h} to meet 8px boundary)\n"
    except ImportError:
        pass  # Pillow not available — let png2asset fail with its own message

    assets_dir = Path("assets")
    assets_dir.mkdir(exist_ok=True)
    out_c = assets_dir / f"{name}.c"
    out_h = assets_dir / f"{name}.h"

    # -map = background tileset+map mode (8x8 tiles); -spr8x8 = hardware sprite mode
    # Array name is derived from the output filename by png2asset (-n is not a valid flag)
    flags = [str(png2asset), image_path, "-o", str(out_c)]
    if sprite:
        flags += ["-spr8x8"]
    else:
        flags += ["-map"]

    try:
        r = subprocess.run(flags, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return "✗ png2asset timed out."

    if image_path.endswith("_padded.png"):
        try:
            Path(image_path).unlink()
        except OSError:
            pass

    if r.returncode != 0:
        return f"✗ png2asset failed:\n{(r.stdout + r.stderr).strip()}"

    lines = [f"✓ Generated {out_c} and {out_h}", padded_note.strip()] if padded_note else [f"✓ Generated {out_c} and {out_h}"]
    if out_c.exists():
        content = out_c.read_text()
        # Show the array declaration so the agent knows what to #include or extern
        for line in content.splitlines():
            if "UINT8" in line or "unsigned char" in line or "extern" in line:
                lines.append(f"  {line.strip()}")
    lines.append(f"\nInclude in your source: #include \"{out_h}\"")
    return "\n".join(lines)


def tool_rom_usage() -> str:
    """Parse the .map file and report ROM/RAM usage per bank."""
    map_files = sorted(Path("build").glob("*.map")) if Path("build").exists() else []
    if not map_files:
        return "✗ No .map file found in build/ — run Build() first."

    map_text = map_files[0].read_text(errors="replace")

    # Parse area lines (ASxxxx format):
    # "_CODE                  00000200    0000154D =        5453. bytes (REL,CON)"
    area_re = re.compile(
        r"^(_\w+)\s+([0-9A-Fa-f]{8})\s+([0-9A-Fa-f]{8})\s+=\s+(\d+)\.",
        re.MULTILINE
    )

    rom_bank0  = 0   # _HOME + _CODE (addr 0x0000–0x3FFF)
    rom_banks: dict[int, int] = {}  # bank N → bytes
    wram       = 0
    hram       = 0

    ROM_HEADER_BYTES = 512  # fixed GB cart header + interrupt vectors (0x0000–0x01FF)
    rom_bank0 = ROM_HEADER_BYTES

    for m in area_re.finditer(map_text):
        area_name = m.group(1)
        addr      = int(m.group(2), 16)
        size      = int(m.group(4))
        if size == 0:
            continue

        # _HEADERx sections are placed by GBDK at absolute addresses in the cart header;
        # their linker addr shows as 0x0000 so skip them — counted in ROM_HEADER_BYTES above.
        if area_name.startswith("_HEADER"):
            continue

        if 0x0000 <= addr <= 0x3FFF:
            rom_bank0 += size
        elif area_name.startswith("_CODE_"):
            bank_num = int(area_name.split("_CODE_")[1]) if "_CODE_" in area_name else 1
            rom_banks[bank_num] = rom_banks.get(bank_num, 0) + size
        elif 0x4000 <= addr <= 0x7FFF:
            # switchable bank — determine bank number from address if name doesn't tell us
            bank_num = int(area_name.split("_CODE_")[1]) if "_CODE_" in area_name else 1
            rom_banks[bank_num] = rom_banks.get(bank_num, 0) + size
        elif 0xC000 <= addr <= 0xDFFF or area_name in ("_DATA", "_BSS", "_INITIALIZED", "_BSEG_DATA"):
            wram += size
        elif 0xFF80 <= addr <= 0xFFFE or area_name in ("_HRAM", "_BSEG"):
            hram += size

    bank0_limit = 16 * 1024
    wram_limit  = 8 * 1024
    hram_limit  = 127

    lines = ["ROM / RAM usage:"]
    pct0 = rom_bank0 / bank0_limit * 100
    lines.append(f"  Bank 0 (fixed): {rom_bank0:,} / {bank0_limit:,} bytes  ({pct0:.1f}%)"
                 + ("  ⚠️  OVERFLOW" if rom_bank0 > bank0_limit else ""))

    for bank_num in sorted(rom_banks):
        b = rom_banks[bank_num]
        pct = b / (16 * 1024) * 100
        lines.append(f"  Bank {bank_num} (switch): {b:,} / 16,384 bytes  ({pct:.1f}%)"
                     + ("  ⚠️  OVERFLOW" if b > 16 * 1024 else ""))

    pct_w = wram / wram_limit * 100
    lines.append(f"  WRAM:           {wram:,} / {wram_limit:,} bytes  ({pct_w:.1f}%)"
                 + ("  ⚠️  OVERFLOW" if wram > wram_limit else ""))

    if hram:
        lines.append(f"  HRAM:           {hram:,} / {hram_limit} bytes")

    return "\n".join(lines)


def tool_symbols(filter: str = "") -> str:
    """Read the .sym file and return symbol addresses grouped by bank."""
    sym_files = sorted(Path("build").glob("*.sym")) if Path("build").exists() else []
    if not sym_files:
        return "✗ No .sym file found in build/ — run Build() first."

    lines_raw = sym_files[0].read_text(errors="replace").splitlines()
    banks: dict[str, list[str]] = {}

    for line in lines_raw:
        line = line.strip()
        if not line or line.startswith(";"):
            continue
        # Format: "00:4000 symbol_name"
        m = re.match(r"^([0-9A-Fa-f]{2}):([0-9A-Fa-f]{4})\s+(\S+)$", line)
        if not m:
            continue
        bank, addr, sym = m.group(1), m.group(2), m.group(3)
        if filter and filter.lower() not in sym.lower():
            continue
        banks.setdefault(bank, []).append(f"  0x{addr}  {sym}")

    if not banks:
        msg = f"No symbols found"
        if filter:
            msg += f" matching '{filter}'"
        return msg + "."

    out = [f"Symbols ({sym_files[0].name}):"]
    for bank in sorted(banks):
        label = f"Bank {int(bank, 16)}" if bank != "00" else "Bank 0 (fixed)"
        out.append(f"\n{label}:")
        out.extend(banks[bank][:50])  # cap per bank to avoid flooding context
        if len(banks[bank]) > 50:
            out.append(f"  ... and {len(banks[bank]) - 50} more")
    return "\n".join(out)


# ── Game Boy C: cpp-based static analysis ──────────────────────────────────────

def _gbdk_include_flags() -> list[str]:
    gbdk_home = os.environ.get("GBDK_HOME", str(Path.home() / "gbdk"))
    inc = Path(gbdk_home) / "include"
    # macOS Apple clang requires -I/path (concatenated) — separate -I and path argv
    # entries are incorrectly treated as a linker input rather than an include dir.
    if inc.exists():
        return [f"-I{inc}", "-D__PORT_sm83", "-D__SDCC"]
    return ["-nostdinc", "-D__PORT_sm83", "-D__SDCC"]


def _cpp_expand(source_file: str) -> str:
    """
    Run cpp -E on a source file, returning macro-expanded text.
    Uses the project cwd so relative includes (assets/*.h) resolve correctly.
    Returns stdout even on non-zero exit — fatal include errors still yield
    partial output with call sites intact.
    """
    cwd = Path(source_file).parent.parent  # src/main.c → project root
    if not cwd.exists():
        cwd = Path.cwd()
    cmd = ["cpp", "-E", "-w"] + _gbdk_include_flags() + [source_file]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20, cwd=str(cwd))
        return r.stdout  # use partial output even if includes fail
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def _safe_eval(expr: str) -> Optional[int]:
    """Evaluate a simple integer arithmetic expression (digits, hex, operators only)."""
    expr = expr.strip()
    if not re.match(r'^[0-9a-fA-FxX\s+\-*/|&~^<>()]+$', expr):
        return None
    try:
        result = eval(compile(expr, "<string>", "eval"), {"__builtins__": {}}, {})
        return int(result) if isinstance(result, (int, float)) else None
    except Exception:
        return None


def _extract_project_defines(source_files: list[str]) -> dict[str, int]:
    """
    Extract and resolve numeric #define constants from project source files.
    Multi-pass substitution handles forward references and arithmetic chains.
    Skips function-like macros, inline comments, and non-integer values.
    """
    raw: dict[str, str] = {}
    for fp in source_files:
        try:
            content = Path(fp).read_text(errors="replace")
        except OSError:
            continue
        # Object-like macros only — function-like have no whitespace before '('
        for m in re.finditer(r'^#define\s+([A-Za-z_]\w*)\s+([^\n\\]+)', content, re.MULTILINE):
            name, raw_val = m.group(1), m.group(2)
            val = re.sub(r'/\*.*?\*/', '', raw_val).split('//')[0].strip()
            if val:
                raw[name] = val

    resolved: dict[str, int] = {}
    changed = True
    passes = 0
    while changed and passes < 8:
        changed = False
        for name, val in raw.items():
            if name in resolved:
                continue
            subbed = re.sub(
                r'\b[A-Z_][A-Z0-9_]*\b',
                lambda m, r=resolved: str(r[m.group(0)]) if m.group(0) in r else m.group(0),
                val
            )
            result = _safe_eval(subbed)
            if result is not None:
                resolved[name] = result
                changed = True
        passes += 1

    return resolved


def _extract_call_args(text: str, func_name: str) -> list[tuple[str, str]]:
    """
    Extract (first_arg, second_arg) from all calls to func_name.
    Paren-aware: correctly handles nested expressions like (BASE + COUNT).
    """
    results = []
    for m in re.finditer(r'\b' + re.escape(func_name) + r'\s*\(', text):
        pos = m.end()
        args: list[str] = []
        buf: list[str] = []
        depth = 1
        while pos < len(text) and depth > 0:
            c = text[pos]
            if c == '(':
                depth += 1
                buf.append(c)
            elif c == ')':
                depth -= 1
                if depth == 0:
                    args.append(''.join(buf).strip())
                else:
                    buf.append(c)
            elif c == ',' and depth == 1:
                args.append(''.join(buf).strip())
                buf = []
            else:
                buf.append(c)
            pos += 1
        if len(args) >= 2:
            results.append((args[0], args[1]))
    return results


def _resolve_expr(expr: str, defines: dict[str, int]) -> Optional[int]:
    """Resolve a C expression to an integer using the given defines dict."""
    expr = expr.strip()
    try:
        return int(expr, 0)
    except ValueError:
        pass
    if re.match(r'^[A-Za-z_]\w*$', expr):
        return defines.get(expr)
    subbed = re.sub(
        r'\b([A-Za-z_]\w*)\b',
        lambda m, d=defines: str(d[m.group(0)]) if m.group(0) in d else m.group(0),
        expr
    )
    return _safe_eval(subbed)


def tool_vram_audit() -> str:
    """Detect VRAM tile index conflicts between background and sprite tile loads."""
    c_files = sorted(Path("src").glob("*.c")) if Path("src").exists() else []
    if not c_files:
        c_files = sorted(Path(".").glob("*.c"))
    if not c_files:
        return "✗ No .c source files found."

    # Build defines dict from all project source and asset headers so _TILE_COUNT
    # values are resolvable even if cpp can't find all includes.
    def_files = (
        [str(p) for p in Path("src").glob("*.[ch]")] +
        ([str(p) for p in Path("assets").glob("*.h")] if Path("assets").exists() else [])
    )
    defines = _extract_project_defines(def_files)

    bkg_ranges: list[tuple[int, int, str]] = []
    spr_ranges: list[tuple[int, int, str]] = []
    unresolved: list[str] = []
    seen: set[tuple[str, int, int]] = set()

    for src in c_files:
        expanded = _cpp_expand(str(src))
        if not expanded:
            return (f"✗ cpp preprocessing failed for {src}. "
                    f"Ensure cpp is installed (Xcode Command Line Tools) and GBDK_HOME is set.")

        for first_s, count_s in _extract_call_args(expanded, "set_bkg_data"):
            first = _resolve_expr(first_s, defines)
            count = _resolve_expr(count_s, defines)
            key = ("bkg", first, count)
            if first is not None and count is not None and count > 0 and key not in seen:
                seen.add(key)
                bkg_ranges.append((first, first + count - 1, src.name))
            elif first is not None and key not in seen:
                seen.add(("bkg_u", first, -1))
                unresolved.append(f"set_bkg_data({first_s}, {count_s}, ...) — count unresolved")

        for first_s, count_s in _extract_call_args(expanded, "set_sprite_data"):
            first = _resolve_expr(first_s, defines)
            count = _resolve_expr(count_s, defines)
            key = ("spr", first, count)
            if first is not None and count is not None and count > 0 and key not in seen:
                seen.add(key)
                spr_ranges.append((first, first + count - 1, src.name))
            elif first is not None and key not in seen:
                seen.add(("spr_u", first, -1))
                unresolved.append(f"set_sprite_data({first_s}, {count_s}, ...) — count unresolved")

    if not bkg_ranges and not spr_ranges:
        return "No set_bkg_data or set_sprite_data calls with resolvable indices found."

    lines = ["VRAM tile index audit:"]

    if bkg_ranges:
        lines.append("\nBackground (set_bkg_data):")
        for first, last, loc in sorted(bkg_ranges):
            lines.append(f"  tiles [{first}–{last}]  ({last - first + 1} tiles)  {loc}")

    if spr_ranges:
        lines.append("\nSprites (set_sprite_data):")
        for first, last, loc in sorted(spr_ranges):
            lines.append(f"  tiles [{first}–{last}]  ({last - first + 1} tiles)  {loc}")

    if unresolved:
        lines.append("\nUnresolved (dynamic indices — verify manually):")
        for u in unresolved:
            lines.append(f"  {u}")

    conflicts = []
    for b0, b1, _ in bkg_ranges:
        for s0, s1, _ in spr_ranges:
            o0, o1 = max(b0, s0), min(b1, s1)
            if o0 <= o1:
                conflicts.append(
                    f"  ⚠️  bkg[{b0}–{b1}] overlaps sprite[{s0}–{s1}] at indices {o0}–{o1}"
                )

    if conflicts:
        bkg_max = max(last for _, last, _ in bkg_ranges)
        lines.append("\nConflicts detected:")
        lines.extend(conflicts)
        lines.append(f"\nFix: move all set_sprite_data() calls to start at index {bkg_max + 1} or higher.")
    elif bkg_ranges and spr_ranges:
        bkg_max = max(last for _, last, _ in bkg_ranges)
        spr_min = min(first for first, _, _ in spr_ranges)
        lines.append(f"\n✓ No conflicts — background ends at {bkg_max}, sprites start at {spr_min}.")
    else:
        lines.append("\n✓ No conflicts detected.")

    return "\n".join(lines)


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
            reason = _bash_block_reason(cmd)
            if reason:
                return (f"Blocked: {reason}"), True
            return tool_bash(**args), False
        if name == "Glob":   return tool_glob(**args),  False
        if name == "Grep":   return tool_grep(**args),  False
        if name == "LS":     return tool_ls(**args),    False
        if name == "Setup":  return tool_setup(**args), False
        if name == "Build":
            result = tool_build()
            return result, result.startswith("✗")
        if name == "TileConvert":
            result = tool_tile_convert(**args)
            return result, result.startswith("✗")
        if name == "RomUsage":
            return tool_rom_usage(), False
        if name == "Symbols":
            return tool_symbols(**args), False
        if name == "VramAudit":
            result = tool_vram_audit()
            return result, ("⚠️" in result or result.startswith("✗"))
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

def _dedup_errors(errors: list[dict]) -> list[dict]:
    seen, unique = set(), []
    for e in errors:
        key = (e["file"], e.get("line", 0), e["code"])
        if key not in seen:
            seen.add(key)
            unique.append(e)
    return unique


def _parse_errors_ts(output: str) -> list[dict]:
    """Parse TypeScript compiler and ESLint errors."""
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
    return _dedup_errors(errors)


def _parse_errors_c(output: str) -> list[dict]:
    """Parse SDCC and GCC errors from make output."""
    errors = []
    # SDCC: src/main.c:42: error 20: Undefined identifier 'foo'
    # SDCC: src/main.c:42: error: message  (no numeric code variant)
    for m in re.finditer(
        r"([^\s:]+\.(?:c|h)):(\d+):\s+error(?:\s+\d+)?:\s+(.+)", output
    ):
        errors.append({
            "file": m.group(1), "line": int(m.group(2)),
            "code": "SDCC", "message": m.group(3).strip()
        })
    # GCC-style (lcc passes these through): src/main.c:42:5: error: message
    for m in re.finditer(
        r"([^\s:]+\.(?:c|h)):(\d+):\d+:\s+error:\s+(.+)", output
    ):
        errors.append({
            "file": m.group(1), "line": int(m.group(2)),
            "code": "GCC", "message": m.group(3).strip()
        })
    # Linker errors: ?ASlink-Error-Undefined Global
    for m in re.finditer(
        r"\?ASlink-Error-(.+)", output
    ):
        errors.append({
            "file": "linker", "line": 0,
            "code": "LINK", "message": m.group(1).strip()
        })
    return _dedup_errors(errors)


def parse_errors(output: str) -> list[dict]:
    """Parse build errors — dispatches to the right parser based on PROFILE."""
    if PROFILE == "gameboy-c":
        return _parse_errors_c(output)
    return _parse_errors_ts(output)

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
    """Pop the checkpoint stash, merging prior agent work back with whatever is
    currently in the worktree. On a merge conflict (round edits overlap prior
    work), keep the worktree version (the more recent fix) and drop the stash so
    we never leave a conflicted tree or a leaked stash."""
    if not ref:
        return
    r = subprocess.run("git stash pop", shell=True, capture_output=True, text=True)
    if r.returncode != 0 and "conflict" in (r.stdout + r.stderr).lower():
        # Resolve in favor of the current worktree, then clear the now-applied stash.
        subprocess.run("git checkout --theirs -- . 2>/dev/null || git checkout -- .",
                       shell=True, capture_output=True)
        subprocess.run("git reset -q", shell=True, capture_output=True)
        subprocess.run("git stash drop", shell=True, capture_output=True)

def diff_since_commit() -> str:
    """Unified diff of the working tree vs HEAD — what the agent has changed so
    far this job (used by the cheat check; the repair checkpoint stash is popped
    back before each verify, so the round's edits are present in the worktree)."""
    r = subprocess.run(
        "git -c core.quotepath=false diff --unified=3 HEAD",
        shell=True, capture_output=True, text=True
    )
    return r.stdout or ""

def _discard_round_edits() -> None:
    """Throw away the current repair round's working-tree edits (the round ran on
    top of a clean HEAD because save_checkpoint stashed prior work away), so the
    following restore_checkpoint pop cleanly reinstates prior work without the
    bad edits. Tracked changes are reset; round-created files are removed."""
    subprocess.run("git checkout -- .", shell=True, capture_output=True)
    subprocess.run("git clean -fdq", shell=True, capture_output=True)

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

# Function-definition matcher shared with _build_repo_map_c: a return type, a
# name, a parameter list, optional GBDK banking attribute, then '{' or ';'.
_C_FN_RE = re.compile(
    r"^(?!#|//|\s*/?\*)(?:[a-zA-Z_][a-zA-Z0-9_\s*]+?)\s+"
    r"[a-zA-Z_]\w*\s*\([^)]*\)\s*(?:NONBANKED|BANKED)?\s*[{;]",
    re.MULTILINE
)

def compress_c_to_symbols(content: str) -> str:
    """Reduce a C/H file to #defines + function signatures + banking attributes.

    The old fallback kept only the first 10 lines — for C that is just #includes,
    the least useful thing to retain. Banking attributes and #defines are exactly
    what linker/VRAM-error diagnosis needs, so preserve those instead.
    """
    out = []
    defines = re.findall(r"^#define\s+[A-Za-z_]\w*\s+[^\n\\]+", content, re.MULTILINE)
    if defines:
        out.extend(d.strip() for d in defines[:40])
        out.append("")
    sigs = [m.group(0).rstrip().rstrip("{").rstrip() for m in _C_FN_RE.finditer(content)]
    # Drop control-flow false positives (if/while/for/switch read as "type name(...)").
    sigs = [s for s in sigs if not re.search(r"\b(if|while|for|switch|return)\s*\($", s)]
    if sigs:
        out.extend(sigs[:60])
    if not out:
        lines = content.splitlines()
        return content if len(lines) <= 15 else "\n".join(lines[:10]) + f"\n... ({len(lines)} lines)"
    return "\n".join(out).strip() + "\n\n[Compressed — use Read for full content]"

def compress_content(content: str, file_path: str) -> str:
    suffix = Path(file_path).suffix
    if suffix in (".ts", ".tsx", ".js", ".jsx"):
        return compress_ts_to_symbols(content)
    if suffix == ".css":
        return compress_css_to_selectors(content)
    if suffix in (".c", ".h"):
        return compress_c_to_symbols(content)
    lines = content.splitlines()
    if len(lines) <= 15:
        return content
    return "\n".join(lines[:10]) + f"\n... ({len(lines)} lines) [use Read for full content]"

def _read_targets(messages: list) -> dict[str, str]:
    """Map each tool_call_id to the file_path of its Read call, for pairing a
    'tool' result message back to the file it read."""
    targets: dict[str, str] = {}
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls", []):
            if tc.get("function", {}).get("name") == "Read":
                try:
                    fp = json.loads(tc["function"].get("arguments", "{}")).get("file_path", "")
                    if fp:
                        targets[tc["id"]] = fp
                except Exception:
                    pass
    return targets

def compress_old_reads(messages: list, keep_recent: int = KEEP_RECENT_TURNS) -> list:
    """
    Shrink the model's working memory without losing what it needs:
    1. Collapse OLDER duplicate Reads of the same file to a one-line stub — the
       'read a section, read it again' pattern accumulates many full copies of
       the same file; only the most recent copy is kept verbatim.
    2. Replace the remaining Read results older than keep_recent with a compressed
       symbol map.
    Reads within the recent window (the model's active context) are untouched.
    """
    cutoff  = max(0, len(messages) - keep_recent)
    targets = _read_targets(messages)

    # Pass 1 — find, for each file, the index of its LAST 'tool' read result, so
    # earlier reads of the same file can be superseded.
    last_read_idx: dict[str, int] = {}
    for i, msg in enumerate(messages):
        if msg.get("role") == "tool":
            fp = targets.get(msg.get("tool_call_id", ""))
            if fp:
                last_read_idx[fp] = i

    compressed = []
    for i, msg in enumerate(messages):
        if msg.get("role") == "tool":
            fp = targets.get(msg.get("tool_call_id", ""))
            if fp:
                if i < last_read_idx.get(fp, i) and i < cutoff:
                    # An older duplicate read of a file that's read again later.
                    msg = {**msg, "content": f"[superseded by a later Read of {fp}]"}
                elif i < cutoff:
                    msg = {**msg, "content": compress_content(msg.get("content", ""), fp)}
        compressed.append(msg)

    return compressed

def maybe_compress(messages: list, real_prompt_tokens: int = 0) -> list:
    """Compress old reads before the model's window fills.

    estimate_tokens (chars/4) is only a lower bound, so when Ollama has told us
    the actual prompt_tokens for the last request we trust that instead, and we
    trigger COMPRESS_MARGIN below the budget so a single fresh tool result can't
    push past the real window between checks.
    """
    used = max(estimate_tokens(messages), real_prompt_tokens)
    if used <= CONTEXT_BUDGET - COMPRESS_MARGIN:
        return messages

    before = estimate_tokens(messages)
    compressed = compress_old_reads(messages)
    after = estimate_tokens(compressed)

    # Only announce when compression actually freed space. Everything over budget
    # may be within the recent window (nothing old to compress) — in that case
    # stay silent and proceed rather than spamming a no-op marker every turn.
    saved = before - after
    if saved > 200:
        amt = f"~{saved // 1000}k" if saved >= 1000 else f"~{saved}"
        emit({"type": "assistant", "message": {"content": [{
            "type": "text",
            "text": f"[Context compressed: freed {amt} tokens from old file reads]"
        }]}})
    return compressed

# ── JSONL event emitter ────────────────────────────────────────────────────────

def emit(event: dict) -> None:
    print(json.dumps(event), flush=True)

# Job-wide running token totals. run_agent_loop is invoked multiple times per job
# (main loop + each repair round) and its local total_in/out reset each call, so
# the LIVE counter accumulates here instead — it only ever climbs across the job.
_session_in = 0
_session_out = 0

def emit_progress(ctx_tokens: int) -> None:
    """Emit a live progress event each turn so the dashboard can show running
    token usage and the current context size vs the compression budget."""
    emit({
        "type": "progress",
        "tokens": {"input": _session_in, "output": _session_out},
        "ctx": {"used": ctx_tokens, "budget": CONTEXT_BUDGET},
    })

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
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT) as resp:
                data = json.loads(resp.read())
            return data["choices"][0]["message"], data.get("usage", {})
        except urllib.error.URLError as e:
            if attempt == 0:
                # Likely context overflow — compress and retry once
                print(f"⚠️  Ollama connection dropped (attempt 1): {e}", file=sys.stderr)
                print("   Compressing context and retrying…", file=sys.stderr)
                compressed = compress_old_reads(messages)
                payload["messages"] = compressed
                req = urllib.request.Request(
                    f"{OLLAMA_HOST}/v1/chat/completions",
                    data=json.dumps(payload).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
            else:
                print(f"❌ Ollama connection failed after retry: {e}", file=sys.stderr)
                print(f"   ollama serve && ollama pull {model}", file=sys.stderr)
                sys.exit(1)

# ── Agent loop ─────────────────────────────────────────────────────────────────

def run_agent_loop(messages: list, model: str, tools: list,
                   max_turns: int = MAX_TURNS,
                   locked_files: Optional[set] = None) -> tuple[list, int, int]:
    """Run tool-use loop until no more tool calls. Returns (messages, in_tok, out_tok)."""
    total_in = total_out = 0
    consecutive_bash = 0  # reset when a Read/Edit/Write happens
    last_read_path = None  # detect re-reading the same file without acting on it
    same_read_count = 0

    global _session_in, _session_out
    last_prompt_tokens = 0  # ground-truth window size from Ollama's last response
    for _ in range(max_turns):
        messages = maybe_compress(messages, last_prompt_tokens)
        msg, usage = call_ollama(messages, model, tools)
        last_prompt_tokens = usage.get("prompt_tokens", 0)
        total_in  += last_prompt_tokens
        total_out += usage.get("completion_tokens", 0)
        # Live progress for the dashboard: job-wide running totals + current
        # context size (the live window vs the compression budget).
        _session_in  += last_prompt_tokens
        _session_out += usage.get("completion_tokens", 0)
        emit_progress(last_prompt_tokens)

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

            # Track re-reads of the same file with no Edit/Write in between: a
            # read→grep→read loop that the Bash-only counter above misses entirely.
            if name in ("Edit", "Write"):
                same_read_count = 0
                last_read_path = None
            elif name == "Read":
                rp = args.get("file_path")
                if rp == last_read_path:
                    same_read_count += 1
                else:
                    last_read_path = rp
                    same_read_count = 1

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

            if same_read_count >= 4:
                result = (
                    f"SPIRAL DETECTED: You have Read {last_read_path} {same_read_count} times "
                    "in a row without editing it. Re-reading will not change the content. "
                    "Use Edit to make the change you planned, or Read a DIFFERENT file. "
                    "If you need a specific section, Read with offset/limit instead of the whole file again."
                )
                emit({"type": "tool_result", "tool_use_id": tc["id"],
                      "content": result, "is_error": True})
                messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
                same_read_count = 0
                continue

            # Detect loop: same call repeated (widened window — small models often
            # interleave one unrelated call to slip past a narrow check).
            recent = [
                m for m in messages[-15:] if m.get("role") == "assistant"
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

        # Verify. The checkpoint stash holds the agent's PRIOR work, so right now
        # the worktree is (clean HEAD + this round's edits) — `git diff HEAD`
        # isolates exactly what this round changed, which the cheat check needs.
        passed, new_errors, _ = run_build()
        new_count = len(new_errors)
        round_diff = diff_since_commit()
        error_locs = {(e["file"], int(e.get("line", 0))) for e in real_errors}
        cheat = repair_cheat_reason(scan_diff(round_diff, error_locs)) if passed else None

        if passed and not cheat:
            emit({"type": "assistant", "message": {"content": [{
                "type": "text", "text": "✅ Build passing after repair."
            }]}})
            # Keep the round's edits AND restore the prior work (merge), then drop
            # nothing — pop already removed the stash. (Previously this `drop`ped
            # the stash, silently discarding all pre-repair agent work.)
            restore_checkpoint(checkpoint)
            return True, messages, total_in, total_out

        if cheat:
            # Build went green by silencing/stubbing the error rather than fixing
            # it. Treat as a regression: discard the round's edits, restore prior
            # work, and escalate to a different strategy.
            emit({"type": "assistant", "message": {"content": [{
                "type": "text",
                "text": f"⚠️  Repair cheated — {cheat}. Reverting and trying a different approach."
            }]}})
            _discard_round_edits()
            restore_checkpoint(checkpoint)
            strategy = min(strategy + 1, len(REPAIR_STRATEGIES) - 1)
        elif new_count > prev_count:
            # Regression — discard the round's bad edits, restore prior work, escalate
            emit({"type": "assistant", "message": {"content": [{
                "type": "text",
                "text": f"⚠️  Repair introduced new errors ({prev_count} → {new_count}). Reverting."
            }]}})
            _discard_round_edits()
            restore_checkpoint(checkpoint)
            strategy = min(strategy + 1, len(REPAIR_STRATEGIES) - 1)
        elif new_count == prev_count:
            # No progress — keep the round's edits, merge prior work back, escalate
            restore_checkpoint(checkpoint)
            strategy = min(strategy + 1, len(REPAIR_STRATEGIES) - 1)
        else:
            # Progress — keep the round's edits, merge prior work back, same strategy
            restore_checkpoint(checkpoint)
            strategy = max(0, strategy - 1)

        errors = new_errors

    return False, messages, total_in, total_out

# ── Main ───────────────────────────────────────────────────────────────────────

def build_repo_map() -> str:
    """Build a compact symbol index of the project for upfront context."""
    if PROFILE == "gameboy-c":
        return _build_repo_map_c()
    return _build_repo_map_ts()


def _build_repo_map_ts() -> str:
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
        for fp in files[:60]:
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


def _build_repo_map_c() -> str:
    try:
        result = subprocess.run(
            r"""find . -type f \( -name "*.c" -o -name "*.h" \) \
                -not -path "*/.git/*" -not -path "*/.claude/*" \
                -not -path "*/build/*" | sort | head -100""",
            shell=True, capture_output=True, text=True, timeout=10
        )
        files = [f for f in result.stdout.strip().splitlines() if f]
        if not files:
            return ""
        fn_re = re.compile(
            r"^(?!#|//|\s*/?\*)(?:[a-zA-Z_][a-zA-Z0-9_\s*]+?)\s+"
            r"([a-zA-Z_]\w*)\s*\([^)]*\)\s*(?:NONBANKED|BANKED)?\s*[{;]",
            re.MULTILINE
        )
        lines = []
        for fp in files[:60]:
            content = Path(fp).read_text(errors="replace")
            fns = [m.group(1) for m in fn_re.finditer(content)
                   if m.group(1) not in ("if", "while", "for", "switch", "return")]
            if fns:
                lines.append(f"{fp[2:]}: {', '.join(dict.fromkeys(fns[:8]))}")

        # Append resolved project-level numeric constants (tile indices, counts, states)
        src_files = [f for f in files if "assets" not in f and "build" not in f]
        defines = _extract_project_defines(src_files[:20])
        if defines:
            const_str = ", ".join(f"{k}={v}" for k, v in sorted(defines.items())[:30])
            lines.append(f"\nProject constants: {const_str}")

        return "\n".join(lines)
    except Exception:
        return ""


def run(request: str, model: str, system_prompt: str) -> int:
    # Select the project's Node version (if it pins one and fnm is present)
    # BEFORE installing deps/building, so install+build run under the right Node.
    # No pin / no fnm → default Node (opt-in by design).
    _node_status = ensure_node_version()
    if _node_status:
        emit({"type": "assistant", "message": {"content": [{
            "type": "text", "text": _node_status
        }]}})

    # Install deps BEFORE the baseline probe. A fresh worktree has no
    # node_modules, so without this the baseline build fails with 'tsc: not
    # found' and the regression gate compares two broken builds (toothless).
    _dep_status = ensure_dependencies()
    if _dep_status:
        emit({"type": "assistant", "message": {"content": [{
            "type": "text", "text": _dep_status
        }]}})

    # Baseline-pin: probe the untouched tree first so the final gate asks "did
    # the agent make the build WORSE", not "is it green". A repo that was already
    # broken at base shouldn't be blamed on the agent, and a green baseline lets
    # us trust that a post-edit failure is the agent's doing.
    base_passed, base_errors, _ = run_build()
    emit({"type": "assistant", "message": {"content": [{
        "type": "text",
        "text": (f"── Baseline build: {'passing' if base_passed else f'already failing ({len(base_errors)} error(s))'} ──")
    }]}})

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
        # Only hold the agent responsible for errors it INTRODUCED. If the repo
        # was already failing at baseline, pre-existing errors aren't the agent's
        # job to fix — repair only the net-new ones so a broken base doesn't trap
        # the loop or fail an otherwise-correct change.
        if not base_passed:
            base_keys = {(e["file"], e.get("line", 0), e.get("code", "")) for e in base_errors}
            new_errors = [e for e in errors if (e["file"], e.get("line", 0), e.get("code", "")) not in base_keys]
            if not new_errors:
                emit({"type": "assistant", "message": {"content": [{
                    "type": "text",
                    "text": (f"⚠️  Build still failing, but all {len(errors)} error(s) pre-existed at baseline — "
                             f"the agent introduced none. Treating as no regression.")
                }]}})
                passed = True
            else:
                emit({"type": "assistant", "message": {"content": [{
                    "type": "text",
                    "text": f"❌ Agent introduced {len(new_errors)} new error(s). Entering surgical repair loop."
                }]}})
                passed, messages, in2, out2 = repair_loop(request, messages, model, new_errors)
                in1 += in2; out1 += out2
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
    prompt_file = Path(prompt_path) if prompt_path else AGENTIC_APP / "agents" / "worker_local.txt"
    if not prompt_file.exists():
        prompt_file = AGENTIC_APP / "agents" / "worker.txt"
    system_prompt = prompt_file.read_text() if prompt_file.exists() else ""

    sys.exit(run(request, MODEL, system_prompt))
