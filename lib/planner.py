#!/usr/bin/env python3
"""
Planning engine for the "planning channels" feature.

A question is a job that may only read. This module is the deterministic,
stdlib-only core that:

  1. Builds a BROADENED symbol map of a repo (internal functions/types/classes,
     not just exports) — a port of lib/utils.sh:_build_repo_map, reusing the
     active profile's symbol_extraction regexes. Cached per-channel and stamped
     with git HEAD; rebuilt only when HEAD moves.
  2. Classifies a question (pure regex) as 'index' (cheap, zero model turns) or
     'agent' (escalate to a read-only agent loop).
  3. Answers index-path questions straight from the map (Tier 0), optionally
     with a single grep for a file:line.
  4. Dispatches the agent path as a SUBPROCESS (never in-process — the local
     worker's SANDBOX_ROOT is import-time captured):
       - cloud: the `claude` CLI with --allowedTools "Read,Grep,Glob,LS"
                --output-format stream-json (NOT claude-api.sh, which has no
                tool loop and cannot ground in files).
       - local: python3 lib/ollama_worker.py --ask with AGENTIC_SANDBOX_ROOT
                set to the repo, cwd=repo.
     Citations are HARVESTED from the read tool calls the agent actually made
     (grounding correct by mechanism, not by prompt discipline).
  5. Derives a proposal of concrete coding jobs (strict-JSON contract) and runs
     TWO-STAGE anchor verification — (A) existence re-grep/re-read drops anchors
     that don't resolve against the live repo; (B) relevance re-Read asks the
     model to confirm each surviving anchor supports the job's claim. A job left
     with zero confirmed anchors is held back. Confirmed anchors are baked into
     the request text in the submit_review_job file:line style.

Hard rules honored:
  - stdlib only (pathlib/json/subprocess/urllib/re) — no web framework, no DB.
  - source/profiles/agents read via AGENTIC_APP; state under AGENTIC_HOME.
  - cloud planning = the `claude` CLI subprocess.
  - the read agent runs as a SUBPROCESS with AGENTIC_SANDBOX_ROOT set.
  - secrets (ANTHROPIC_API_KEY) are passed in the child env only, never returned.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

# ── Paths (state under AGENTIC_HOME, source under AGENTIC_APP) ───────────────────

AGENTIC_HOME = Path(os.environ.get("AGENTIC_HOME", Path.home() / ".agentic"))
# agents/ + profiles/ are APP SOURCE — read from AGENTIC_APP (defaults to
# AGENTIC_HOME for native installs; Docker bakes /opt/agentic).
AGENTIC_APP = Path(os.environ.get("AGENTIC_APP", str(AGENTIC_HOME)))
CHANNELS_DIR = AGENTIC_HOME / "channels"
PYTHON = os.environ.get("AGENTIC_PYTHON", str(AGENTIC_HOME / "venv" / "bin" / "python3"))

PLANNING_MAX_TURNS_DEFAULT = 8

# Lang profile loader — optional so planner can be used standalone in tests.
try:
    sys.path.insert(0, str(Path(__file__).parent))
    from lang_profile import load_profile as _load_profile  # type: ignore
except Exception:  # pragma: no cover - defensive
    def _load_profile(name: str) -> dict[str, Any]:  # type: ignore[misc]
        return {}

# settings.get_secret — optional so a missing module never crashes planning.
try:
    import settings as _settings  # type: ignore
except Exception:  # pragma: no cover - defensive
    _settings = None  # type: ignore


def _get_secret(name: str) -> str:
    if _settings is not None:
        try:
            return _settings.get_secret(name)
        except Exception:
            pass
    return os.environ.get(name, "")


# ── Broadened symbol map (port of utils.sh:_build_repo_map) ──────────────────────

# Built-in fallbacks — broadened beyond the export-only profile regexes to also
# catch INTERNAL functions/types/classes for TS and gameboy-c. Used when a
# profile supplies no symbol_extraction, and unioned with the profile regexes
# when it does (so we never lose the profile's named_export/reexport behavior).
_TS_BROAD = (
    # internal + exported function / const-arrow / class / interface / type / enum
    r"^\s*(?:export\s+)?(?:(?:async|default|declare|abstract)\s+)*"
    r"(?:function\*?\s+|const\s+|let\s+|var\s+|class\s+|interface\s+|type\s+|enum\s+)(\w+)"
)
_TS_REEXPORT = r"^\s*export\s+\{([^}]+)\}"
# C: a function definition `ret name(args) {` or `ret name(args) BANKED {`,
# broadened to also catch typedef/struct/enum/union declarations.
_C_BROAD_FUNC = (
    r"^(?!\s*//)[a-zA-Z_][a-zA-Z0-9_*\s]+\s+(\w+)\s*\([^;{]*\)\s*"
    r"(?:NONBANKED|BANKED)?\s*\{"
)
_C_BROAD_TYPE = r"^\s*(?:typedef\s+(?:struct|enum|union)?|struct|enum|union)\s+(\w+)"

# Default exclude dirs (mirrors lang_profile._builtin_default + utils.sh defaults).
_DEFAULT_EXCLUDES = {
    "node_modules", ".git", "dist", "build", ".next", ".claude", "coverage",
    "__pycache__", ".turbo", "out", ".vercel", "worktrees", "queue", "obj",
    "res", "bin",
}


def _broad_regexes(profile: dict[str, Any]) -> list[re.Pattern[str]]:
    """Return the list of compiled symbol-name regexes for this profile.

    Always unions the profile's own symbol_extraction regexes (named_export,
    reexport) with the broadened internal-symbol patterns so the map is a
    superset of today's export-only map — never a regression.
    """
    name = (profile or {}).get("name", "")
    exts = tuple((profile or {}).get("source_extensions", []))
    pats: list[str] = []

    sym = (profile or {}).get("symbol_extraction", {}) or {}
    if sym.get("named_export"):
        pats.append(sym["named_export"])
    if sym.get("reexport"):
        pats.append(sym["reexport"])

    is_c = name == "gameboy-c" or any(e in (".c", ".h") for e in exts)
    if is_c:
        pats += [_C_BROAD_FUNC, _C_BROAD_TYPE]
    else:
        # Default to the TS/JS broadened set (also a sane fallback for unknown
        # profiles, which usually ship .ts/.tsx defaults).
        pats += [_TS_BROAD, _TS_REEXPORT]

    out: list[re.Pattern[str]] = []
    for p in pats:
        try:
            out.append(re.compile(p, re.MULTILINE))
        except re.error:
            continue
    return out


def _extract_symbols(content: str, regexes: list[re.Pattern[str]]) -> list[str]:
    """Pull unique symbol names from one file's content using all regexes.

    Reexport patterns capture a brace list (`{a, b as c}`); split those and
    strip ` as ` aliases, exactly like _build_repo_map.
    """
    names: list[str] = []
    for rx in regexes:
        for m in rx.finditer(content):
            grp = m.group(1)
            if grp is None:
                continue
            if "," in grp or " as " in grp:
                for part in grp.split(","):
                    n = part.strip().split(" as ")[0].strip()
                    if n and n not in ("default", ""):
                        names.append(n)
            else:
                names.append(grp.strip())
    seen: set[str] = set()
    unique: list[str] = []
    for n in names:
        if n and n not in seen:
            seen.add(n)
            unique.append(n)
    return unique


def build_symbol_map(repo: str, profile: Optional[str] = None) -> str:
    """Build a 'relpath: sym1, sym2' map of the repo, broadened to internal
    symbols. Walks with cwd=repo so relative paths match the worktree the agent
    will read. Returns a newline-joined string (possibly empty)."""
    repo_path = Path(repo)
    prof = _load_profile(profile or "typescript")
    exts = tuple(prof.get("source_extensions", [".ts", ".tsx"]))
    excludes = set(prof.get("exclude_dirs", [])) or set(_DEFAULT_EXCLUDES)
    regexes = _broad_regexes(prof)

    lines: list[str] = []
    for root, dirs, files in os.walk(repo_path):
        dirs[:] = sorted(d for d in dirs if d not in excludes and not d.startswith("."))
        for fname in sorted(files):
            if not any(fname.endswith(ext) for ext in exts):
                continue
            fpath = Path(root) / fname
            try:
                rel = str(fpath.relative_to(repo_path))
            except ValueError:
                rel = str(fpath)
            try:
                content = fpath.read_text(errors="replace")
            except Exception:
                continue
            syms = _extract_symbols(content, regexes)
            if syms:
                lines.append(f"{rel}: {', '.join(syms)}")
    return "\n".join(lines)


def _git_head(repo: str) -> str:
    try:
        r = subprocess.run(
            ["git", "-C", repo, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def cached_symbol_map(cid: str, repo: str, profile: Optional[str] = None,
                      force: bool = False) -> tuple[str, str]:
    """Return (symbol_map_text, head_sha), rebuilding the on-disk cache only when
    git HEAD moved (or force=True / cache missing). Cache lives at
    channels/<cid>.symbols with a leading '# head: <sha>' stamp line.

    Cheap by design: a `git rev-parse HEAD` plus a stamp compare on the common
    path; a full walk only when the index is actually stale."""
    head = _git_head(repo)
    cache = CHANNELS_DIR / f"{cid}.symbols"
    if not force and cache.exists():
        try:
            text = cache.read_text(errors="replace")
        except Exception:
            text = ""
        first, _, body = text.partition("\n")
        if first.startswith("# head: "):
            stamped = first[len("# head: "):].strip()
            if stamped and stamped == head:
                return body, head
    # (Re)build.
    body = build_symbol_map(repo, profile)
    try:
        CHANNELS_DIR.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_suffix(".symbols.tmp")
        tmp.write_text(f"# head: {head}\n{body}")
        os.replace(tmp, cache)
    except Exception:
        pass  # cache is an optimization; planning still works without it
    return body, head


def symbol_map_stats(symbol_map: str) -> dict[str, int]:
    """Quick counts for the UI header ('Index: N symbols across M files')."""
    files = 0
    symbols = 0
    for line in symbol_map.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        _, _, rhs = line.partition(": ")
        files += 1
        symbols += len([s for s in rhs.split(",") if s.strip()])
    return {"files": files, "symbols": symbols}


# ── Classification (pure regex — free, inspectable; never an LLM) ────────────────

# Escalation triggers: behavior / how / why / trace / what-calls.
_ESCALATE_RE = re.compile(
    r"\b(how|why|should|explain|trace|debug|what\s+happens|what\s+calls|"
    r"who\s+calls|when\s+does|where\s+does|walk\s+me|reason|cause|flow|"
    r"behav|implement(?:ed|ation)?|work[s]?\b|interact|depend)\b",
    re.IGNORECASE,
)
# Index-answerable: enumerate/locate/existence questions over the symbol map.
_INDEX_RE = re.compile(
    r"\b(what|which|list|where\s+is|does|is\s+there|are\s+there|find|show\s+me)\b"
    r".*\b(export|exports|function|functions|component|components|hook|hooks|"
    r"type|types|file|files|class|classes|interface|interfaces|enum|enums|"
    r"symbol|symbols|defined|exist|exists)\b",
    re.IGNORECASE | re.DOTALL,
)
# "where is <Symbol>" / "does <Symbol> exist" — pure locate/existence over the
# index even without a trailing kind noun (doc §Step 1 lists these explicitly).
_LOCATE_RE = re.compile(
    r"^\s*(?:where\s+is|where\s+are|does\s+\w+\s+exist|is\s+there\s+a)\b",
    re.IGNORECASE,
)


def classify(question: str) -> str:
    """Return 'index' or 'agent' purely from the question text (no model).

    Rules (doc §Step 1):
      - 'how/why/should/explain/trace/what calls' or anything naming behavior
        => 'agent' (escalate). Behavior wins even if enumeration words appear.
      - 'what/which … (export|function|component|hook|type|file)s …', 'list …',
        'where is X', 'does X exist' => 'index'.
      - default => 'agent' (when in doubt, ground in real files).
    """
    q = (question or "").strip()
    if not q:
        return "agent"
    if _ESCALATE_RE.search(q):
        return "agent"
    if _LOCATE_RE.search(q) or _INDEX_RE.search(q):
        return "index"
    return "agent"


# ── Tier-0 index answer (zero model turns) ───────────────────────────────────────

_SYMBOLISH = re.compile(r"\b([A-Za-z_]\w*)\b")


def _grep_symbol(repo: str, symbol: str, max_hits: int = 1) -> list[dict[str, Any]]:
    """One cheap grep for a symbol → up to max_hits {file, line, text}.

    Used by the index path only to attach a single file:line citation. Runs with
    cwd=repo so the returned paths are repo-relative."""
    cits: list[dict[str, Any]] = []
    if not symbol:
        return cits
    try:
        r = subprocess.run(
            ["grep", "-rnw", "--", symbol, "."],
            cwd=repo, capture_output=True, text=True, timeout=20,
        )
    except Exception:
        return cits
    for raw in r.stdout.splitlines():
        # grep -rn output: ./path/to/file:line:content
        m = re.match(r"^\.?/?([^:]+):(\d+):(.*)$", raw)
        if not m:
            continue
        f = m.group(1)
        if any(seg in _DEFAULT_EXCLUDES for seg in f.split("/")):
            continue
        cits.append({"file": f, "start": int(m.group(2)),
                     "end": int(m.group(2)), "why": f"defines/uses {symbol}"})
        if len(cits) >= max_hits:
            break
    return cits


def _search_symbol_map(symbol_map: str, needle: str) -> list[tuple[str, str]]:
    """Return [(file, 'sym1, sym2')] lines whose file path OR symbol list
    contains the needle (case-insensitive substring / word match)."""
    out: list[tuple[str, str]] = []
    nlow = needle.lower()
    for line in symbol_map.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        file, _, syms = line.partition(": ")
        symset = [s.strip() for s in syms.split(",") if s.strip()]
        if nlow in file.lower() or any(nlow == s.lower() or nlow in s.lower() for s in symset):
            out.append((file, syms))
    return out


def answer_index(question: str, symbol_map: str, repo: str) -> dict[str, Any]:
    """Tier-0 answer from the symbol map (+ at most one grep for file:line).

    Returns {answer, grounding:'index', citations:[...], hits:int, badge}. The
    caller treats hits==0 or hits>40 as "ambiguous → escalate" per the doc
    (this function reports `hits` and `escalate` so the caller can decide)."""
    q = (question or "").strip()
    # Candidate identifiers in the question, preferring capitalized / camel /
    # snake tokens (likely symbol names) over common words.
    tokens = [t for t in _SYMBOLISH.findall(q)]
    stop = {
        "what", "which", "where", "is", "the", "a", "an", "are", "in", "under",
        "list", "does", "exist", "exists", "of", "to", "for", "and", "or",
        "find", "show", "me", "all", "there", "file", "files", "function",
        "functions", "export", "exports", "component", "components", "hook",
        "hooks", "type", "types", "class", "classes", "symbol", "symbols",
        "defined", "interface", "interfaces", "enum", "enums",
    }
    cands = [t for t in tokens if t.lower() not in stop]
    # Prefer symbol-shaped tokens.
    cands.sort(key=lambda t: (not (t[0].isupper() or "_" in t or any(c.isupper() for c in t[1:])), len(t)), reverse=False)

    matched: list[tuple[str, str]] = []
    used: Optional[str] = None
    for c in cands:
        hits = _search_symbol_map(symbol_map, c)
        if hits:
            matched = hits
            used = c
            break

    n = len(matched)
    citations: list[dict[str, Any]] = []
    if used and n and not (n > 40):
        # Attach one grounding file:line for the best symbol.
        citations = _grep_symbol(repo, used, max_hits=1)

    if n == 0:
        return {
            "answer": "No matching files or symbols found in the index.",
            "grounding": "index", "citations": [], "hits": 0,
            "escalate": True, "badge": "index • free",
        }

    # Compose a compact answer listing matching files + symbols (cap to keep it
    # legible; the badge advertises the zero-cost path).
    shown = matched[:40]
    body_lines = [f"{f}: {syms}" for f, syms in shown]
    extra = "" if n <= 40 else f"\n... and {n - 40} more files (refine the query)"
    answer = (
        f"Found {n} matching file(s) in the index"
        + (f" for `{used}`" if used else "")
        + ":\n" + "\n".join(body_lines) + extra
    )
    return {
        "answer": answer, "grounding": "index", "citations": citations,
        "hits": n, "escalate": (n > 40), "badge": "index • free",
    }


# ── Stream parsing + citation harvesting (claude CLI + ollama --ask) ─────────────

def _iter_stream_events(stdout_lines) -> Any:
    """Yield parsed JSON event dicts from a stream-json line iterator,
    tolerating non-JSON noise and the SOH-prefixed PROGRESS sentinel."""
    for raw in stdout_lines:
        line = raw.strip()
        if not line or line.startswith("\x01"):
            continue
        try:
            yield json.loads(line)
        except Exception:
            continue


def _harvest_event(ev: dict[str, Any], citations: list[dict[str, Any]],
                   files_read: set, stats: dict[str, int],
                   final_answer: Optional[dict[str, str]] = None) -> str:
    """Process one stream event for citations/answer/tokens. Returns any
    assistant text in the event (concatenated by the caller).

    Handles BOTH wire shapes:
      - ollama_worker.py / stream_parser flat events: {type:'tool_use', name,
        input}, {type:'assistant', message:{content:[{type:'text',...}]}}, and a
        final {type:'answer', text:...} (the clean finished answer).
      - claude CLI: {type:'assistant', message:{content:[{type:'tool_use',
        name, input}, {type:'text', text}]}} and {type:'result', usage|result}.
    """
    text_out = ""
    t = ev.get("type", "")
    # Carries the symbol from a search tool_use to its following tool_result, so
    # the result's file:line citations can be tagged with the symbol.
    _pending_symbol = _harvest_event._pending  # list[str] holder, persists across calls

    # Tools whose INPUT names a real FILE (file_path) we should cite directly.
    _FILE_READ_TOOLS = ("Read", "ReadSymbol", "Outline")
    # Tools that SEARCH for a symbol; their input `path` is a DIRECTORY (or '.'),
    # NOT the file the symbol lives in. We must NOT cite that dir — the real file
    # comes from the tool's OUTPUT (file:line lines), harvested in _record_result.
    _SYMBOL_SEARCH_TOOLS = ("Signature", "Usages", "Grep")

    def _record_read(name: str, inp: dict[str, Any]) -> None:
        if name in _FILE_READ_TOOLS:
            path = inp.get("file_path") or inp.get("path") or inp.get("file") or ""
            # Only cite a real file, never a directory (e.g. a stray "src/hooks").
            if path and not str(path).rstrip("/").endswith(("/",)) and "." in os.path.basename(str(path)):
                files_read.add(path)
                start = inp.get("offset") or inp.get("start")
                limit = inp.get("limit")
                end = None
                if start and limit:
                    try:
                        end = int(start) + int(limit) - 1
                    except (TypeError, ValueError):
                        end = None
                cit = {"file": str(path), "why": f"opened via {name}"}
                if start:
                    try:
                        cit["start"] = int(start)
                        cit["end"] = int(end) if end else int(start)
                    except (TypeError, ValueError):
                        pass
                # Symbol-scoped reads (ReadSymbol) cite a whole file but are "about"
                # one symbol — carry it so the peek can highlight where it lives.
                sym = inp.get("name") or inp.get("symbol") or ""
                if sym:
                    cit["symbol"] = str(sym)
                citations.append(cit)
        elif name in _SYMBOL_SEARCH_TOOLS:
            # Record the symbol/pattern; the actual file:line is harvested from the
            # tool's output (_record_result), not the input search dir.
            pat = inp.get("symbol") or inp.get("pattern") or ""
            if pat:
                _pending_symbol[0] = str(pat)

    def _record_result(name: str, content: str) -> None:
        """Harvest real file:line citations from a symbol-search tool's OUTPUT.
        Signature/Usages/Grep emit lines like 'path/to/file.ts:42: <code>'."""
        sym = _pending_symbol[0]
        for raw in (content or "").splitlines():
            m = re.match(r"^\.?/?([^\s:][^:]*?):(\d+):", raw)
            if not m:
                continue
            f, ln = m.group(1), int(m.group(2))
            # Skip our own savings-note / non-path noise.
            if "." not in os.path.basename(f):
                continue
            files_read.add(f)
            cit = {"file": f, "start": ln, "end": ln,
                   "why": f"found via {name}" + (f" ({sym})" if sym else "")}
            if sym:
                cit["symbol"] = sym
            citations.append(cit)
        _pending_symbol[0] = ""

    if t == "tool_use":  # flat (ollama) shape
        _record_read(ev.get("name", ""), ev.get("input", {}) or {})
    elif t == "tool_result":  # flat (ollama): result of the preceding tool_use
        if _pending_symbol[0]:  # a symbol-search is awaiting its file:line output
            _record_result("search", str(ev.get("content", "")))
    elif t == "assistant":
        for block in ev.get("message", {}).get("content", []) or []:
            bt = block.get("type")
            if bt == "text":
                text_out += block.get("text", "")
            elif bt == "tool_use":  # claude CLI nests tool_use here
                _record_read(block.get("name", ""), block.get("input", {}) or {})
    elif t == "user":  # claude CLI nests tool_result in a 'user' message
        for block in ev.get("message", {}).get("content", []) or []:
            if block.get("type") == "tool_result" and _pending_symbol[0]:
                content = block.get("content", "")
                if isinstance(content, list):
                    content = "\n".join(b.get("text", "") for b in content if isinstance(b, dict))
                _record_result("search", str(content))
    elif t == "answer":  # ollama worker's clean finished answer
        if final_answer is not None and isinstance(ev.get("text"), str):
            final_answer["text"] = ev["text"]
    elif t == "result":
        usage = ev.get("usage", {}) or {}
        stats["input"] = stats.get("input", 0) + int(usage.get("input_tokens", 0) or 0)
        stats["output"] = stats.get("output", 0) + int(usage.get("output_tokens", 0) or 0)
        # claude CLI also surfaces a final result string sometimes:
        if isinstance(ev.get("result"), str):
            if final_answer is not None:
                final_answer["text"] = ev["result"]
            elif not text_out:
                text_out += ev["result"]
    elif t == "progress":  # ollama live token counter
        tok = ev.get("tokens", {}) or {}
        stats["input"] = max(stats.get("input", 0), int(tok.get("input", 0) or 0))
        stats["output"] = max(stats.get("output", 0), int(tok.get("output", 0) or 0))
    return text_out

# Per-run holder: the symbol from the last search tool_use, awaiting its result.
# A 1-element list so the inner closures can mutate it across _harvest_event calls.
_harvest_event._pending = [""]  # type: ignore[attr-defined]


def _dedup_citations(citations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set = set()
    out: list[dict[str, Any]] = []
    for c in citations:
        key = (c.get("file"), c.get("start"), c.get("end"), c.get("pattern"))
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


# ── Agent dispatch (SUBPROCESS — never in-process) ───────────────────────────────

def _planner_prompt() -> str:
    """Read agents/planner.txt from AGENTIC_APP; empty string if absent."""
    p = AGENTIC_APP / "agents" / "planner.txt"
    try:
        return p.read_text()
    except Exception:
        return ""


def _child_env(repo: str, secret_anthropic_key: bool = True) -> dict[str, str]:
    """Build the child env. ANTHROPIC_API_KEY is resolved server-side and passed
    here ONLY — it is never returned to a caller / browser."""
    env = dict(os.environ)
    env["AGENTIC_HOME"] = str(AGENTIC_HOME)
    env["AGENTIC_APP"] = str(AGENTIC_APP)
    env["AGENTIC_SANDBOX_ROOT"] = os.path.realpath(repo)
    if secret_anthropic_key:
        # CLOUD path. The ambient env (copied above) may carry the Ollama
        # backend's ANTHROPIC_BASE_URL=http://localhost:11434 and
        # ANTHROPIC_AUTH_TOKEN=ollama, exported by lib/config.sh in the
        # no-.agentic.conf (Docker) case. If they leak through, the claude CLI
        # authenticates with the real key but dials Ollama's port →
        # ConnectionRefused. Drop them so the CLI uses api.anthropic.com,
        # then let an explicit ANTHROPIC_BASE_URL secret override if present.
        env.pop("ANTHROPIC_BASE_URL", None)
        env.pop("ANTHROPIC_AUTH_TOKEN", None)
        key = _get_secret("ANTHROPIC_API_KEY")
        if key:
            env["ANTHROPIC_API_KEY"] = key
        base = _get_secret("ANTHROPIC_BASE_URL")
        if base:
            env["ANTHROPIC_BASE_URL"] = base
    return env


def _build_agent_cmd(mode: str, model: str, request: str, repo: str,
                     max_turns: int,
                     seed_map: str = "") -> tuple[list[str], dict[str, str]]:
    """Return (argv, env) for the read-only agent subprocess.

    cloud -> the `claude` CLI (a real tool loop) with read-only tools and
             stream-json output, exactly like the cloud job worker but with the
             write tools stripped. NOT claude-api.sh (single-shot curl, no loop).
             The symbol map is inlined into the prompt (the CLI has no seed env).
    local -> python3 lib/ollama_worker.py --ask with AGENTIC_SANDBOX_ROOT=repo.
             The symbol map is passed via AGENTIC_ASK_PREAMBLE so the worker
             formats it as project context; the question stays clean.
    """
    system_prompt = _planner_prompt()
    if mode == "cloud":
        prompt = request
        if seed_map:
            prompt = (
                f"{request}\n\n"
                "Repository symbol map (file: symbols) — use it to jump straight "
                "to the right files; prefer the cheapest tool that answers:\n"
                f"{seed_map[:8000]}"
            )
        cmd = [
            "claude", "-p", prompt,
            "--dangerously-skip-permissions",
            "--allowedTools", "Read,Grep,Glob,LS",
            "--output-format", "stream-json",
            "--verbose",
        ]
        if system_prompt:
            cmd[3:3] = ["--system-prompt", system_prompt]
        if model and model != "auto":
            cmd += ["--model", model]
        return cmd, _child_env(repo, secret_anthropic_key=True)
    # local — ollama_worker.py --ask reads the question as argv[2] and takes its
    # turn cap + model + planner prompt + symbol-map preamble from the env
    # (AGENTIC_PLANNING_MAX_TURNS / AGENTIC_LOCAL_MODEL / AGENTIC_PLANNER_PROMPT /
    # AGENTIC_ASK_PREAMBLE). SANDBOX_ROOT is import-time captured there, so this
    # MUST be a subprocess with AGENTIC_SANDBOX_ROOT set.
    worker = str(AGENTIC_APP / "lib" / "ollama_worker.py")
    cmd = [PYTHON, worker, "--ask", request]
    env = _child_env(repo, secret_anthropic_key=False)
    env["AGENTIC_PLANNING_MAX_TURNS"] = str(max_turns)
    if model:
        env["AGENTIC_LOCAL_MODEL"] = model
    # The worker reads its planner persona from AGENTIC_PLANNER_PROMPT (falls back
    # to AGENTIC_APP/agents/planner.txt on its own); set it explicitly.
    env["AGENTIC_PLANNER_PROMPT"] = str(AGENTIC_APP / "agents" / "planner.txt")
    if seed_map:
        env["AGENTIC_ASK_PREAMBLE"] = seed_map[:8000]
    return cmd, env


def run_agent(mode: str, model: str, request: str, repo: str,
              max_turns: int = PLANNING_MAX_TURNS_DEFAULT,
              timeout: int = 600,
              seed_map: str = "",
              on_event: Optional[Any] = None) -> dict[str, Any]:
    """Dispatch a read-only agent subprocess, stream-parse its output, and
    harvest citations from the read tool calls it actually made.

    seed_map — repository symbol map routed to the right channel per backend
    (inlined into the cloud prompt; AGENTIC_ASK_PREAMBLE env for local).
    on_event(ev_dict) — optional callback for live SSE streaming (called for
    every parsed stream event before harvesting).

    Returns {answer, grounding:'agent', citations, files_read, tokens,
    turns, exit_code}.
    """
    cmd, env = _build_agent_cmd(mode, model, request, repo, max_turns, seed_map)
    citations: list[dict[str, Any]] = []
    files_read: set = set()
    stats: dict[str, int] = {"input": 0, "output": 0}
    answer_parts: list[str] = []
    final_answer: dict[str, str] = {"text": ""}
    _harvest_event._pending = [""]  # reset per-run search-symbol holder
    turns = 0

    try:
        proc = subprocess.Popen(
            cmd, cwd=repo, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
    except FileNotFoundError as e:
        return {
            "answer": f"Planning agent could not start ({mode}): {e}",
            "grounding": "agent", "citations": [], "files_read": [],
            "tokens": {"input": 0, "output": 0}, "turns": 0, "exit_code": 127,
            "error": str(e),
        }

    try:
        assert proc.stdout is not None
        for raw in proc.stdout:
            for ev in _iter_stream_events([raw]):
                if ev.get("type") in ("assistant", "tool_use"):
                    turns += 1
                if on_event is not None:
                    try:
                        on_event(ev)
                    except Exception:
                        pass
                answer_parts.append(
                    _harvest_event(ev, citations, files_read, stats, final_answer)
                )
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
    code = proc.returncode if proc.returncode is not None else -1

    # Prefer the clean final answer event; fall back to concatenated turns.
    answer = (final_answer["text"] or "".join(answer_parts)).strip()
    if not answer and code != 0:
        try:
            err = (proc.stderr.read() if proc.stderr else "")[:500]
        except Exception:
            err = ""
        answer = f"Planning agent ({mode}) produced no answer (exit {code}). {err}".strip()

    return {
        "answer": answer,
        "grounding": "agent",
        "citations": _dedup_citations(citations),
        "files_read": sorted(files_read),
        "tokens": {"input": stats.get("input", 0), "output": stats.get("output", 0)},
        "turns": turns,
        "exit_code": code,
    }


def _percent_saved_hint(files_read: int, turns: int) -> str:
    """A stable, percent-based 'saved vs naive read' hint for the agent badge —
    a recommendation-with-reason, not real-time accounting (doc §Cost signals).
    Heuristic: the curated efficient tools are assumed to save ~60% vs reading
    whole files; scale gently with how few files were opened."""
    if files_read <= 0:
        return "read 0 files"
    base = 60  # mid-point of the curated-tool savings band (60–95%)
    # Fewer files opened to answer => relatively more saved vs a naive full read.
    bonus = max(0, 20 - turns)
    pct = min(90, base + bonus)
    return f"read {files_read} file(s) · {turns} turn(s) · ~{pct}% saved vs naive read"


# ── Tiered ask() — the public grounding entry point ──────────────────────────────

def ask(channel: dict[str, Any], thread: dict[str, Any], question: str,
        symbol_map: Optional[str] = None, dig_deeper: bool = False,
        on_event: Optional[Any] = None) -> dict[str, Any]:
    """Run the tiered grounding flow for one question.

    channel: {cid, repo, profile, ...}; thread: {planning_mode, planning_model,
    planning_max_turns?}. `symbol_map` may be passed (already cached) or this
    builds/loads it.

    Returns a uniform answer dict:
      {answer, grounding:'index'|'agent', citations:[...], tokens:{in,out},
       turns, badge, percent_saved_hint, files_read}
    """
    repo = channel["repo"]
    cid = channel.get("cid", "")
    profile = channel.get("profile")
    if symbol_map is None:
        symbol_map, _ = cached_symbol_map(cid, repo, profile) if cid else (build_symbol_map(repo, profile), "")

    bucket = "agent" if dig_deeper else classify(question)

    if bucket == "index":
        idx = answer_index(question, symbol_map, repo)
        if not idx.get("escalate"):
            return {
                "answer": idx["answer"],
                "grounding": "index",
                "citations": idx["citations"],
                "tokens": {"input": 0, "output": 0},
                "turns": 0,
                "files_read": [c["file"] for c in idx["citations"] if c.get("file")],
                "badge": idx["badge"],
                "percent_saved_hint": "index • free",
            }
        # ambiguous (0 or >40 hits) → fall through to the agent path

    mode = thread.get("planning_mode", channel.get("default_mode", "local"))
    model = thread.get("planning_model", "")
    max_turns = int(thread.get("planning_max_turns", PLANNING_MAX_TURNS_DEFAULT) or PLANNING_MAX_TURNS_DEFAULT)

    # Seed the agent with the symbol map (routed per-backend by run_agent) so it
    # knows where things live and can jump straight to the right files.
    res = run_agent(mode, model, question, repo, max_turns=max_turns,
                    seed_map=symbol_map or "", on_event=on_event)
    files_read = res.get("files_read", [])
    return {
        "answer": res["answer"],
        "grounding": "agent",
        "citations": res["citations"],
        "tokens": res["tokens"],
        "turns": res["turns"],
        "files_read": files_read,
        "badge": f"read {len(files_read)} files · {res['turns']} turns",
        "percent_saved_hint": _percent_saved_hint(len(files_read), res["turns"]),
    }


# ── Derivation: strict-JSON jobs + two-stage anchor verification ─────────────────

def _derive_prompt() -> str:
    p = AGENTIC_APP / "agents" / "planner_derive.txt"
    try:
        return p.read_text()
    except Exception:
        # Self-contained fallback so derivation works even before the prompt
        # file ships (slice 3).
        return (
            "Propose a MINIMAL set of concrete coding jobs from this conversation. "
            "Split independent areas into separate jobs; sequence dependent work "
            "as a chain via depends_on. For EACH job, open the real files and "
            "include exact file:line anchors that support the change. "
            "Return ONLY a JSON array, no prose, of objects shaped:\n"
            '[{"title":"...","request":"<plain text>","depends_on":<earlier '
            'index or null>,"anchors":[{"file":"path","start":30,"end":50}]}]'
        )


def _extract_json_array(text: str) -> list[dict[str, Any]]:
    """Pull the first top-level JSON array out of a possibly-noisy agent answer.
    Tolerates code fences and leading prose."""
    if not text:
        return []
    # Strip code fences.
    cleaned = re.sub(r"```(?:json)?", "", text)
    start = cleaned.find("[")
    if start < 0:
        return []
    depth = 0
    for i in range(start, len(cleaned)):
        ch = cleaned[i]
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                blob = cleaned[start:i + 1]
                try:
                    data = json.loads(blob)
                    return data if isinstance(data, list) else []
                except Exception:
                    return []
    return []


def _read_anchor_range(repo: str, file: str, start: int, end: int,
                       pad: int = 0) -> Optional[str]:
    """Read lines [start, end] (1-based, inclusive) of repo/file, or None if the
    file/range doesn't resolve against the live repo."""
    fp = (Path(repo) / file)
    if not fp.exists() or not fp.is_file():
        return None
    try:
        lines = fp.read_text(errors="replace").splitlines()
    except Exception:
        return None
    total = len(lines)
    s = max(1, int(start) - pad)
    e = min(total, int(end) + pad)
    if s > total:
        return None
    return "\n".join(lines[s - 1:e])


def verify_anchor_existence(repo: str, anchor: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Stage A — existence. Drop anchors that don't resolve against the live
    repo. Anchors are read-pointers: prefer the symbol name + a small range. If
    a `symbol` is named, re-grep for it and snap the range to the first hit
    (HEAD may have shifted since the agent read it). Returns a normalized anchor
    or None."""
    file = str(anchor.get("file", "")).strip()
    if not file:
        return None
    symbol = str(anchor.get("symbol", "")).strip()
    start = anchor.get("start")
    end = anchor.get("end", start)

    fp = Path(repo) / file
    if not fp.exists() or not fp.is_file():
        # Try to relocate the symbol elsewhere in the repo before dropping.
        if symbol:
            hits = _grep_symbol(repo, symbol, max_hits=1)
            if hits:
                h = hits[0]
                return {"file": h["file"], "start": h["start"], "end": h["start"],
                        "symbol": symbol}
        return None

    # File exists. If a symbol is named, snap the range to where it lives now.
    if symbol:
        try:
            content = fp.read_text(errors="replace").splitlines()
        except Exception:
            content = []
        for i, ln in enumerate(content, start=1):
            if re.search(rf"\b{re.escape(symbol)}\b", ln):
                return {"file": file, "start": i, "end": i, "symbol": symbol}
        # Symbol named but not found in the file → don't resolve.
        return None

    # No symbol: validate the numeric range exists.
    if start is None:
        # Whole-file pointer is acceptable as long as the file resolves.
        return {"file": file, "start": 1, "end": 1}
    try:
        s = int(start)
        e = int(end) if end is not None else s
    except (TypeError, ValueError):
        return {"file": file, "start": 1, "end": 1}
    if _read_anchor_range(repo, file, s, e) is None:
        return None
    return {"file": file, "start": s, "end": e}


def verify_anchor_relevance(repo: str, job_title: str, job_request: str,
                            anchor: dict[str, Any], mode: str, model: str,
                            timeout: int = 60) -> str:
    """Stage B — relevance (ADVISORY, not a gate). Re-Read the anchor's range and
    ask the planning model whether it's relevant to read for the job. Returns a
    tri-state verdict: 'relevant' | 'irrelevant' | 'unknown'.

    This is NOT a hard pass/fail filter — anchors are read-pointers ("read around
    here"), so a partial snippet is normal and a small local model frequently
    says NO even when it IS relevant. So: we let the model REASON, then parse the
    last explicit verdict, and FAIL OPEN — only an explicit NO marks 'irrelevant';
    a call failure / no clear verdict is 'unknown' (kept, just unconfirmed). The
    caller never drops an anchor on this; it only sets a confidence badge."""
    snippet = _read_anchor_range(repo, anchor["file"], anchor["start"], anchor["end"], pad=4)
    if snippet is None:
        return "unknown"
    prompt = (
        "Decide if a code region is relevant CONTEXT to read for a coding task. "
        "The region is a pointer ('read around here'), not the full change, so a "
        "partial match still counts as relevant.\n\n"
        f"TASK: {job_title}\n{job_request}\n\n"
        f"CODE REGION ({anchor['file']}:{anchor['start']}-{anchor['end']}):\n"
        f"{snippet}\n\n"
        "Briefly explain in one sentence whether this region is relevant context "
        "for the task, then end your reply with a final line that is exactly "
        "'VERDICT: RELEVANT' or 'VERDICT: IRRELEVANT'."
    )
    try:
        if mode == "cloud":
            out = _confirm_via_claude_cli(prompt, model, repo, timeout)
        else:
            out = _confirm_via_ollama(prompt, model, timeout)
    except _RelevanceCallError:
        return "unknown"          # infra failure — fail OPEN, keep the anchor
    except Exception:
        return "unknown"
    up = (out or "").upper()
    if not up.strip():
        return "unknown"
    # Parse the explicit verdict; prefer the LAST one if the model rambled.
    verdicts = re.findall(r"VERDICT:\s*(RELEVANT|IRRELEVANT)", up)
    if verdicts:
        return "irrelevant" if verdicts[-1] == "IRRELEVANT" else "relevant"
    # No structured verdict — fall back to loose signal, fail open.
    if re.search(r"\bIRRELEVANT\b|\bNOT RELEVANT\b", up):
        return "irrelevant"
    if re.search(r"\bRELEVANT\b|\bYES\b", up):
        return "relevant"
    return "unknown"


class _RelevanceCallError(Exception):
    """The relevance model call failed at the infrastructure level (timeout,
    unreachable host, HTTP error) — as opposed to the model returning a verdict.
    Lets the caller FAIL OPEN (keep the anchor as 'unknown') instead of treating
    an outage as an explicit 'irrelevant'."""


def _confirm_via_claude_cli(prompt: str, model: str, repo: str, timeout: int) -> str:
    """Single-shot claude CLI text call for the relevance check (no tools)."""
    cmd = ["claude", "-p", prompt, "--output-format", "text"]
    if model and model != "auto":
        cmd += ["--model", model]
    env = _child_env(repo, secret_anthropic_key=True)
    try:
        r = subprocess.run(cmd, cwd=repo, env=env, capture_output=True,
                           text=True, timeout=timeout)
    except Exception as e:
        raise _RelevanceCallError(str(e))
    return r.stdout or ""


def _confirm_via_ollama(prompt: str, model: str, timeout: int) -> str:
    """Single-shot Ollama chat for the relevance check (no tools). Raises
    _RelevanceCallError on an infrastructure failure so the caller can fail open."""
    host = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
    # Default to the SAME model the rest of the app uses (qwen-coder:latest), not
    # a stale hardcoded tag — the in-process planner has no AGENTIC_LOCAL_MODEL.
    mdl = (model or os.environ.get("AGENTIC_LOCAL_MODEL")
           or _default_local_model())
    payload = {
        "model": mdl,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "options": {"think": False},
    }
    req = urllib.request.Request(
        f"{host}/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        raise _RelevanceCallError(str(e))
    try:
        return data["choices"][0]["message"].get("content", "") or ""
    except (KeyError, IndexError, TypeError):
        return ""


def _default_local_model() -> str:
    """The configured local model, matching settings.local_model (qwen-coder:latest
    default) so the relevance check uses the same model as everything else."""
    try:
        import settings as _s
        return _s.load().get("local_model") or "qwen-coder:latest"
    except Exception:
        return "qwen-coder:latest"


def _anchors_from_prose(text: str) -> list[dict[str, Any]]:
    """Fallback: pull `path/to/file.ext:START[-END]` anchors out of request prose
    when the model wrote them in text but not in the structured array. Matches a
    path with a file extension followed by :line or :start-end."""
    out: list[dict[str, Any]] = []
    seen: set = set()
    # e.g. src/components/SearchPanel.tsx:30-48  or  lib/foo.py:42
    for m in re.finditer(r"([A-Za-z0-9_./\-]+\.[A-Za-z0-9]+):(\d+)(?:-(\d+))?", text):
        f, s, e = m.group(1), int(m.group(2)), m.group(3)
        key = (f, s)
        if key in seen:
            continue
        seen.add(key)
        out.append({"file": f, "start": s, "end": int(e) if e else s})
    return out


def bake_anchors_into_request(request: str, anchors: list[dict[str, Any]]) -> str:
    """Inline confirmed anchors into the request text in the submit_review_job
    file:line style (job_queue.py:submit_review_job), so the runner sees only a
    self-contained request string — never the thread (decision 2: isolation)."""
    if not anchors:
        return request
    lines = []
    for a in anchors:
        f = a["file"]
        s = a.get("start")
        e = a.get("end", s)
        if s and e and s != e:
            loc = f"{f}:{s}-{e}"
        elif s:
            loc = f"{f}:{s}"
        else:
            loc = f
        why = a.get("why") or a.get("symbol")
        lines.append(f"- `{loc}`" + (f" — {why}" if why else ""))
    return (
        f"{request.strip()}\n\n"
        "Context to read first (verified against the codebase):\n"
        + "\n".join(lines)
    )


def derive(channel: dict[str, Any], thread: dict[str, Any], transcript: str,
           on_event: Optional[Any] = None) -> dict[str, Any]:
    """Run the derivation agent + two-stage anchor verification.

    transcript: the thread conversation text to derive jobs from (the engine is
    transcript-in / proposal-out; channels.py owns persistence).

    Returns a proposal dict:
      {jobs:[{seq,title,request,depends_on,anchors,confirmed,held_back}],
       held_back:[...], raw_count:int}
    Jobs with zero confirmed anchors are flagged held_back=True (kept for the UI
    to surface as "needs a human anchor" rather than shown as grounded).
    """
    repo = channel["repo"]
    mode = thread.get("planning_mode", channel.get("default_mode", "local"))
    model = thread.get("planning_model", "")
    max_turns = int(thread.get("planning_max_turns", PLANNING_MAX_TURNS_DEFAULT) or PLANNING_MAX_TURNS_DEFAULT)

    request = (
        f"{_derive_prompt()}\n\n"
        "=== CONVERSATION TO DERIVE JOBS FROM ===\n"
        f"{transcript}\n"
        "=== END CONVERSATION ===\n"
        "Open the real files to confirm your anchors, then return ONLY the JSON array."
    )
    res = run_agent(mode, model, request, repo, max_turns=max_turns, on_event=on_event)
    raw_jobs = _extract_json_array(res["answer"])

    jobs_out: list[dict[str, Any]] = []
    for seq, j in enumerate(raw_jobs):
        if not isinstance(j, dict):
            continue
        title = str(j.get("title", f"Job {seq + 1}")).strip()
        req = str(j.get("request", "")).strip()
        depends_on = j.get("depends_on")
        if isinstance(depends_on, bool) or not isinstance(depends_on, int):
            depends_on = None
        raw_anchors = j.get("anchors") or []
        # Prose-anchor fallback: if the model put anchors in the request TEXT but
        # not in the structured array, harvest file:line[-range] from the prose so
        # they aren't lost.
        if not raw_anchors:
            raw_anchors = _anchors_from_prose(req)

        # Stage A — EXISTENCE (the trustworthy gate). An anchor that resolves
        # against the live repo counts. This alone decides whether a job is
        # grounded; relevance is advisory only.
        existing: list[dict[str, Any]] = []
        for a in raw_anchors:
            if not isinstance(a, dict):
                continue
            norm = verify_anchor_existence(repo, a)
            if norm:
                norm["why"] = a.get("why") or a.get("note")
                existing.append(norm)

        # Stage B — RELEVANCE (ADVISORY, never drops an anchor). Annotates each
        # anchor with a verdict for a confidence badge. Small local models answer
        # "no" to partial read-pointer snippets even when relevant, so this must
        # NOT gate. Skip entirely if there's nothing to confirm.
        relevant_count = 0
        for a in existing:
            verdict = verify_anchor_relevance(repo, title, req, a, mode, model)
            a["relevance"] = verdict        # 'relevant' | 'irrelevant' | 'unknown'
            if verdict == "relevant":
                relevant_count += 1

        # held_back is purely about EXISTENCE now: a job is only "needs a human
        # anchor" if it genuinely has NO resolvable file:line at all.
        held_back = len(existing) == 0
        # Confidence: high if some anchor was confirmed relevant; otherwise
        # "unverified" (still grounded, just not relevance-confirmed).
        confidence = "high" if relevant_count else ("unverified" if existing else "none")
        reason = None
        if held_back:
            reason = ("no anchors in the model's output" if not raw_anchors
                      else "anchors did not resolve against the repo")

        baked = bake_anchors_into_request(req, existing) if existing else req
        jobs_out.append({
            "seq": seq,
            "title": title,
            "request": baked,
            "depends_on": depends_on,
            "anchors": existing,
            "confidence": confidence,        # high | unverified | none
            "confirmed": not held_back,
            "held_back": held_back,
            "held_back_reason": reason,
        })

    return {
        "jobs": jobs_out,
        "held_back": [j["seq"] for j in jobs_out if j["held_back"]],
        "raw_count": len(raw_jobs),
        "grounding": res["grounding"],
        "tokens": res["tokens"],
    }


# ── Model lists helper (both lists, ungated) ─────────────────────────────────────

def model_lists() -> dict[str, list[str]]:
    """Return {cloud:[...], local:[...]} — both lists always, regardless of the
    global mode (the /api/channels/models endpoint removes the is_local gate).
    Reuses job_queue.fetch_models / get_ollama_models when importable, with
    static fallbacks so this never raises."""
    cloud: list[str] = []
    local: list[str] = []
    try:
        import job_queue as _jq  # type: ignore
        try:
            cloud = list(_jq.fetch_models())
        except Exception:
            cloud = list(getattr(_jq, "FALLBACK_MODELS", []))
        try:
            local = list(_jq.get_ollama_models())
        except Exception:
            local = []
    except Exception:
        cloud = ["auto", "claude-opus-4-8", "claude-opus-4-7",
                 "claude-sonnet-4-6", "claude-haiku-4-5-20251001"]
        local = []
    return {"cloud": cloud, "local": local}


# ── Self-test ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":  # pragma: no cover
    here = str(Path(__file__).resolve().parent)  # lib/ — full of source files
    print("== build_symbol_map(self) ==")

    # This repo is bash + Python (no .ts/.tsx), so to PROVE the walk + broadened
    # extraction machinery on this repo we drive build_symbol_map with an ad-hoc
    # Python-aware profile dict via the same engine the public API uses. The
    # public profile contract (TS/gameboy-c) is unchanged.
    py_profile = {
        "name": "python-selftest",
        "source_extensions": [".py"],
        "exclude_dirs": list(_DEFAULT_EXCLUDES) + ["venv"],
        "symbol_extraction": {
            "named_export": r"^\s*(?:async\s+)?(?:def|class)\s+(\w+)",
        },
    }
    _mod = sys.modules[__name__]
    _orig_loader = _mod._load_profile
    _mod._load_profile = lambda _name: py_profile  # type: ignore[attr-defined]
    try:
        smap = build_symbol_map(here, "python-selftest")
    finally:
        _mod._load_profile = _orig_loader  # type: ignore[attr-defined]

    stats = symbol_map_stats(smap)
    print(f"python map: {stats['files']} files, {stats['symbols']} symbols")
    sample = "\n".join(smap.splitlines()[:5])
    if sample:
        print(sample)
    map_ok = stats["symbols"] > 0
    print("symbol map non-empty:", "OK" if map_ok else "XX")

    print("\n== classify() ==")
    cases = [
        ("what functions are in lib/planner.py", "index"),
        ("which components are exported under src/ui", "index"),
        ("where is build_symbol_map", "index"),
        ("does answer_index exist", "index"),
        ("list the hooks in src/hooks", "index"),
        ("how does the repair loop work", "agent"),
        ("why does the worker compress context", "agent"),
        ("what happens when HEAD moves", "agent"),
        ("trace the job submission flow", "agent"),
        ("what calls submit_job", "agent"),
        ("explain the symbol map", "agent"),
    ]
    ok = True
    for q, expected in cases:
        got = classify(q)
        mark = "OK " if got == expected else "XX "
        if got != expected:
            ok = False
        print(f"  {mark} [{got:5}] (want {expected:5})  {q}")

    print(f"\nclassify: {'ALL PASS' if ok else 'FAILURES PRESENT'}")
    sys.exit(0 if (ok and map_ok) else 1)
