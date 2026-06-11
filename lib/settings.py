"""Runtime settings for the local-model worker — the JSON config layer that
replaces the shell-sourced .agentic.conf for behavior knobs.

Two files under AGENTIC_HOME:
  settings.json  (0644) — non-sensitive behavior knobs, UI-editable, safe to
                          serve to the browser.
  secrets.json   (0600) — API key / provider auth, NEVER sent to the browser.

Resolution order per knob: built-in default → env var → settings.json. The UI
(settings.json) is authoritative — a knob you set in the panel overrides any
leftover AGENTIC_* env var, so "configure in the UI" actually takes effect. The
env var is only a fallback that seeds a value for a key the UI hasn't set yet
(keeps a fresh / pre-migration setup working). With both files absent the
defaults are a runnable local-mode config, so a fresh install lands on a working
UI to configure from.

Pure stdlib. Imported by ollama_worker (reads knobs fresh per job) and serve
(GET/POST endpoints). The schema is the single source of truth — the UI gets its
controls, bounds, and help text from here.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Optional

AGENTIC_HOME = Path(os.environ.get("AGENTIC_HOME", Path.home() / ".agentic"))
SETTINGS_FILE = AGENTIC_HOME / "settings.json"
SECRETS_FILE  = AGENTIC_HOME / "secrets.json"
LEGACY_CONF   = AGENTIC_HOME / ".agentic.conf"


# ── Knob schema ─────────────────────────────────────────────────────────────────
# Each knob: default, the env var that overrides it, a type, validation bounds,
# the UI group, a control hint, and one line of help. Defaults match the worker's
# current values exactly, so absent files = unchanged behavior.
#
# group:   context | model | caps | timeout   (UI sections)
# control: slider | number | text | select

SCHEMA: dict[str, dict[str, Any]] = {
    # ── Run mode + target ──
    "mode": {
        "default": "local", "env": "AGENTIC_MODE", "type": "str",
        "options": ["local", "cloud"],
        "group": "mode", "control": "select", "label": "Job execution mode",
        "help": "How the WORKER runs queued jobs: locally (Ollama) or via the Claude "
                "API (cloud). Global — applies to every job. Separate from a planning "
                "channel's backend, which you pick per thread. Takes effect on the next job.",
    },
    "default_repo": {
        "default": "", "env": "AGENTIC_DEFAULT_REPO", "type": "str",
        "group": "mode", "control": "dirpicker", "label": "Default project path",
        "help": "Project new jobs default to. Click Browse to pick a git repo. "
                "Empty = the directory the server started in. In Docker, paths are "
                "browsed under the mounted BROWSE_ROOT (default your home dir).",
    },
    # ── Context + loop core ──
    "context_budget": {
        "default": 24000, "env": "AGENTIC_CONTEXT_BUDGET", "type": "int",
        "min": 8000, "max": 200000, "step": 4000,
        "group": "context", "control": "slider", "label": "Context budget",
        "help": "Compress old reads once the conversation passes this many tokens. "
                "Keep it below your model's num_ctx window.",
    },
    "keep_recent_turns": {
        "default": 15, "env": "AGENTIC_KEEP_RECENT_TURNS", "type": "int",
        "min": 4, "max": 60, "step": 1,
        "group": "context", "control": "slider", "label": "Keep recent turns",
        "help": "How many recent reads stay uncompressed (the model's active context).",
    },
    "max_turns": {
        "default": 60, "env": "AGENTIC_MAX_TURNS", "type": "int",
        "min": 10, "max": 200, "step": 5,
        "group": "context", "control": "slider", "label": "Max turns per job",
        "help": "Hard cap on agent turns before a job stops.",
    },
    # ── Planning channels (read-only grounding agent) ──
    "planning_max_turns": {
        "default": 8, "env": "AGENTIC_PLANNING_MAX_TURNS", "type": "int",
        "min": 1, "max": 40, "step": 1,
        "group": "planning", "control": "slider", "label": "Planning max turns",
        "help": "Hard cap on read-agent turns when answering a planning-channel "
                "question (the only cap on a planning thread). Lower than jobs' "
                "Max turns because grounding a question should be cheap.",
    },
    "planning_default_mode": {
        "default": "local", "env": "AGENTIC_PLANNING_DEFAULT_MODE", "type": "str",
        "options": ["local", "cloud"],
        "group": "planning", "control": "select", "label": "Planning default backend",
        "help": "Backend a new planning thread starts on — Ollama (local) or the "
                "Claude CLI (cloud). Decoupled from Execution mode: you can plan "
                "with cloud while jobs run local, or vice-versa. Editable per thread.",
    },
    "planning_default_model": {
        "default": "", "env": "AGENTIC_PLANNING_DEFAULT_MODEL", "type": "str",
        "group": "planning", "control": "text", "label": "Planning default model",
        "help": "Model a new planning thread starts on. Empty = let the chosen "
                "backend pick its default. Editable per thread.",
    },
    "compress_margin": {
        "default": 4000, "env": "AGENTIC_COMPRESS_MARGIN", "type": "int",
        "min": 0, "max": 20000, "step": 1000,
        "group": "context", "control": "number", "label": "Compress margin",
        "help": "Trigger compression this far below the budget, so a fresh tool "
                "result can't overshoot the window between checks.",
    },
    # ── Model + Ollama ──
    "local_model": {
        "default": "qwen-coder:latest", "env": "AGENTIC_LOCAL_MODEL", "type": "str",
        "group": "model", "control": "select", "label": "Local model",
        "help": "The Ollama model jobs run against.",
    },
    "cloud_model": {
        "default": "auto", "env": "AGENTIC_MODEL", "type": "str",
        "group": "cloud", "control": "select", "label": "Cloud model",
        "help": "The Claude model cloud jobs run against. 'auto' lets the CLI pick. "
                "The list reflects your account (set the API key above to fetch it).",
    },
    "ollama_keep_alive": {
        "default": "30m", "env": "OLLAMA_KEEP_ALIVE", "type": "str",
        "group": "model", "control": "text", "label": "Ollama keep-alive",
        "help": "How long Ollama keeps the model resident after a request (e.g. 30m, 1h, -1 = forever).",
    },
    "ollama_max_loaded": {
        "default": 3, "env": "OLLAMA_MAX_LOADED_MODELS", "type": "int",
        "min": 1, "max": 8, "step": 1,
        "group": "model", "control": "number", "label": "Max loaded models",
        "help": "How many models Ollama may hold in memory at once.",
    },
    # ── Tool output caps ──
    "read_max_lines": {
        "default": 400, "env": "AGENTIC_READ_MAX_LINES", "type": "int",
        "min": 50, "max": 5000, "step": 50,
        "group": "caps", "control": "number", "label": "Read max lines",
        "help": "Files longer than this are head-truncated with a 'read a range' marker. "
                "Raise it if your large context wants whole files.",
    },
    "bash_max_chars": {
        "default": 4000, "env": "AGENTIC_BASH_MAX_CHARS", "type": "int",
        "min": 1000, "max": 40000, "step": 1000,
        "group": "caps", "control": "number", "label": "Bash output cap",
        "help": "Max characters of a Bash result fed back to the model.",
    },
    "grep_max_chars": {
        "default": 4000, "env": "AGENTIC_GREP_MAX_CHARS", "type": "int",
        "min": 1000, "max": 40000, "step": 1000,
        "group": "caps", "control": "number", "label": "Grep output cap",
        "help": "Max characters of a Grep result fed back to the model.",
    },
    # ── Job timeout ──
    "ollama_timeout": {
        "default": 1800, "env": "AGENTIC_OLLAMA_TIMEOUT", "type": "int",
        "min": 60, "max": 7200, "step": 60,
        "group": "timeout", "control": "number", "label": "Ollama timeout (s)",
        "help": "How long to wait on a single generation before killing the job.",
    },
}


_INVALID = object()  # sentinel: a value that couldn't be coerced

def _coerce(spec: dict[str, Any], value: Any) -> Any:
    """Coerce a raw value to the knob's type, or return _INVALID for junk."""
    try:
        if spec["type"] == "int":
            return int(value)
        return str(value)
    except (TypeError, ValueError):
        return _INVALID


def _clamp(spec: dict[str, Any], value: Any) -> Any:
    """Clamp an int knob to its bounds; constrain a select knob to its options;
    pass other strings through."""
    if spec["type"] == "int" and "min" in spec and "max" in spec:
        return max(spec["min"], min(spec["max"], value))
    if spec.get("options") and value not in spec["options"]:
        return spec["default"]
    return value


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _parse_legacy_conf() -> dict[str, str]:
    """Parse `export VAR="value"` lines from the legacy .agentic.conf."""
    out: dict[str, str] = {}
    try:
        for line in LEGACY_CONF.read_text().splitlines():
            line = line.strip()
            if not line.startswith("export ") or "=" not in line:
                continue
            name, _, val = line[len("export "):].partition("=")
            out[name.strip()] = val.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def _write_settings(stored: dict[str, Any]) -> None:
    AGENTIC_HOME.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(stored, indent=2))
    try:
        os.chmod(SETTINGS_FILE, 0o644)
    except OSError:
        pass


def migrate_from_conf_if_needed() -> bool:
    """One-time, non-destructive bridge: if settings.json doesn't exist yet but a
    legacy .agentic.conf does, import its knobs into settings.json and its
    key/auth into secrets.json (0600). The .conf is left untouched as a fallback.
    Returns True if a migration was performed. Writes files DIRECTLY (not via
    save/load) to avoid re-entering load()."""
    if SETTINGS_FILE.exists() or not LEGACY_CONF.exists():
        return False
    conf = _parse_legacy_conf()
    if not conf:
        return False
    # Knobs: map each schema env var present in the conf into settings.json.
    knobs: dict[str, Any] = {}
    for key, spec in SCHEMA.items():
        if spec["env"] in conf:
            c = _coerce(spec, conf[spec["env"]])
            if c is not _INVALID:
                knobs[key] = _clamp(spec, c)
    _write_settings(knobs)  # always create the file so migration runs once
    # Secrets: key + provider base url move to the 0600 secrets file.
    if conf.get("ANTHROPIC_API_KEY"):
        set_secret("ANTHROPIC_API_KEY", conf["ANTHROPIC_API_KEY"])
    if conf.get("ANTHROPIC_BASE_URL"):
        set_secret("ANTHROPIC_BASE_URL", conf["ANTHROPIC_BASE_URL"])
    return True


def load() -> dict[str, Any]:
    """Resolve every knob: default → settings.json → env var (env wins)."""
    migrate_from_conf_if_needed()
    stored = _read_json(SETTINGS_FILE)
    out: dict[str, Any] = {}
    for key, spec in SCHEMA.items():
        val = spec["default"]
        # env is a FALLBACK (seeds a value for a key not yet set in the UI)...
        env = os.environ.get(spec["env"])
        if env is not None and env != "":
            c = _coerce(spec, env)
            if c is not _INVALID:
                val = _clamp(spec, c)
        # ...and settings.json (the UI) is AUTHORITATIVE — a knob set in the panel
        # overrides any leftover legacy AGENTIC_* env var, so "configure in the UI"
        # actually takes effect.
        if key in stored:
            c = _coerce(spec, stored[key])
            if c is not _INVALID:
                val = _clamp(spec, c)
        out[key] = val
    return out


def get(key: str) -> Any:
    """Resolve a single knob (convenience for the worker)."""
    return load()[key]


def save(updates: dict[str, Any]) -> dict[str, Any]:
    """Validate + persist knob updates to settings.json (0644). Unknown keys are
    ignored; values are coerced and clamped. Returns the full resolved settings."""
    stored = _read_json(SETTINGS_FILE)
    for key, raw in (updates or {}).items():
        spec = SCHEMA.get(key)
        if spec is None:
            continue  # never let an unknown/secret key into settings.json
        c = _coerce(spec, raw)
        if c is _INVALID:
            continue  # reject junk — keep the prior value
        if spec.get("options") and c not in spec["options"]:
            continue  # reject an out-of-options value — keep the prior value
        stored[key] = _clamp(spec, c)
    _write_settings(stored)
    return load()


def model_num_ctx(model: Optional[str] = None) -> Optional[int]:
    """The model's built num_ctx (Modelfile PARAMETER), so the UI can cap the
    context-budget slider at the actual window. Returns None if unavailable."""
    name = model or load().get("local_model") or ""
    if not name:
        return None
    try:
        out = subprocess.run(
            ["ollama", "show", name, "--modelfile"],
            capture_output=True, text=True, timeout=5,
        ).stdout
        m = re.search(r"^PARAMETER\s+num_ctx\s+(\d+)", out, re.MULTILINE)
        if m:
            return int(m.group(1))
        # Fall back to the model's max context length if no explicit num_ctx.
        out2 = subprocess.run(
            ["ollama", "show", name], capture_output=True, text=True, timeout=5,
        ).stdout
        m2 = re.search(r"context length\s+(\d+)", out2)
        if m2:
            return int(m2.group(1))
    except Exception:
        pass
    return None


def schema_for_ui() -> list[dict[str, Any]]:
    """The schema + current values, for the settings panel to render controls."""
    current = load()
    rows = []
    for key, spec in SCHEMA.items():
        row = {k: v for k, v in spec.items() if k != "env" and k != "default"}
        row["key"] = key
        row["value"] = current[key]
        rows.append(row)
    return rows


# ── Secrets (separate file, 0600, never served to the browser) ──────────────────

def get_secret(name: str) -> str:
    return str(_read_json(SECRETS_FILE).get(name, "")) or os.environ.get(name, "")


def set_secret(name: str, value: str) -> None:
    data = _read_json(SECRETS_FILE)
    if value:
        data[name] = value
    else:
        data.pop(name, None)
    AGENTIC_HOME.mkdir(parents=True, exist_ok=True)
    SECRETS_FILE.write_text(json.dumps(data, indent=2))
    try:
        os.chmod(SECRETS_FILE, 0o600)
    except OSError:
        pass


def secrets_status() -> dict[str, bool]:
    """Which secrets are set (booleans only) — safe to send to the browser."""
    data = _read_json(SECRETS_FILE)
    return {
        "anthropic_api_key": bool(data.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")),
        "anthropic_base_url": data.get("ANTHROPIC_BASE_URL") or os.environ.get("ANTHROPIC_BASE_URL", ""),
    }
