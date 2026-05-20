#!/usr/bin/env python3
"""
agentic queue dashboard — serve.py
Full local web UI: submit, run worker, view diff, accept/reject — no terminal needed.
"""

import atexit
import http.server
import json
import os
import pathlib
import random
import signal
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error

# ── Paths ──────────────────────────────────────────────────────────────────────

AGENTIC_HOME  = pathlib.Path(os.environ.get("AGENTIC_HOME", pathlib.Path.home() / ".agentic"))
QUEUE_DIR     = AGENTIC_HOME / "queue"
WORKTREES_DIR = AGENTIC_HOME / "worktrees"
STATES        = ("pending", "running", "done", "failed", "abandoned", "cancelled")

# ── Queue helpers ──────────────────────────────────────────────────────────────

def queue_init() -> None:
    for state in STATES:
        (QUEUE_DIR / state).mkdir(parents=True, exist_ok=True)
    WORKTREES_DIR.mkdir(parents=True, exist_ok=True)


def new_job_id() -> str:
    suffix = format(random.randint(0, 0xFFFF), "04x")
    return "j_" + time.strftime("%Y%m%d_%H%M%S") + "_" + suffix


def submit_job(request: str, repo: str, priority: int, model_hint: str, after: str = None) -> str:
    result = subprocess.run(
        ["git", "-C", repo, "rev-parse", "--git-dir"],
        capture_output=True,
    )
    if result.returncode != 0:
        raise ValueError(f"Not a git repository: {repo}")

    job_id       = new_job_id()
    now_iso      = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    submitted_by = f"{os.uname().nodename}:{os.getpid()}"
    ts           = time.strftime("%Y%m%d_%H%M%S")

    # Record the branch at submit time so accept always merges into the right place
    base_result  = subprocess.run(
        ["git", "-C", repo, "symbolic-ref", "--short", "HEAD"],
        capture_output=True, text=True,
    )
    base_branch  = base_result.stdout.strip() or "HEAD"

    job = {
        "id": job_id, "request": request, "target_repo": repo,
        "model_hint": model_hint, "priority": priority,
        "base_branch": base_branch,
        "parent_request_id": after or None,
        "submitted_at": now_iso, "submitted_by": submitted_by,
        "state_history": [{"state": "pending", "at": now_iso}],
        "summary": None,
    }
    filename = f"{priority}_{ts}_{job_id}.json"
    (QUEUE_DIR / "pending" / filename).write_text(json.dumps(job, indent=2))
    return job_id


def find_job(job_id: str, states=None):
    """Return (data_dict, file_path) for a job, searching the given states."""
    for state in (states or STATES):
        d = QUEUE_DIR / state
        if not d.is_dir():
            continue
        for f in d.glob("*.json"):
            try:
                data = json.loads(f.read_text())
                if data.get("id") == job_id:
                    data["_state"] = state
                    return data, f
            except Exception:
                pass
    raise ValueError(f"Job not found: {job_id}")


def cancel_job(job_id: str) -> None:
    _, f = find_job(job_id, states=["pending"])
    data = json.loads(f.read_text())
    now  = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    data.setdefault("state_history", []).append({"state": "cancelled", "at": now})
    data["summary"] = data.get("summary") or "cancelled via dashboard"
    (QUEUE_DIR / "cancelled" / f.name).write_text(json.dumps(data, indent=2))
    f.unlink()


def accept_job(job_id: str) -> str:
    """Checkout base_branch in target_repo, merge agentic/<id> into it, remove worktree."""
    job, _ = find_job(job_id, states=["done"])
    target       = job["target_repo"]
    branch       = f"agentic/{job_id}"
    wt           = WORKTREES_DIR / job_id
    base_branch  = job.get("base_branch") or "HEAD"

    # Always merge into the branch that was active when the job was submitted —
    # this means the user never needs to switch branches before clicking Accept.
    if base_branch != "HEAD":
        co = subprocess.run(
            ["git", "-C", target, "checkout", base_branch],
            capture_output=True, text=True,
        )
        if co.returncode != 0:
            raise RuntimeError(f"Could not checkout {base_branch}: {co.stderr.strip()}")

    r = subprocess.run(
        ["git", "-C", target, "merge", branch, "--no-ff", "-m", f"Accept agentic job: {job_id}"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "git merge failed")

    if wt.exists():
        subprocess.run(
            ["git", "-C", target, "worktree", "remove", str(wt), "--force"],
            capture_output=True,
        )
    return f"Merged {branch} into {base_branch}"


def accept_chain(job_id: str) -> dict:
    """
    Collect all done jobs in the chain into a single staging branch.
    Creates agent-work/<date>-<short-id> from the base branch, merges each
    agentic/<id> branch into it in order, then removes worktrees.
    The user then merges the staging branch into their own branch when ready.
    """
    all_jobs = read_jobs()

    # Walk forward through done descendants
    chain, current = [job_id], job_id
    while True:
        child = next(
            (j["id"] for j in all_jobs
             if j.get("parent_request_id") == current and j["_state"] == "done"),
            None,
        )
        if not child:
            break
        chain.append(child)
        current = child

    # Use the first job's base branch and target repo
    first_job, _ = find_job(chain[0])
    target      = first_job["target_repo"]
    base_branch = first_job.get("base_branch") or "HEAD"
    stamp       = time.strftime("%Y%m%d")
    short_id    = chain[0][-4:]
    staging     = f"agent-work/{stamp}-{short_id}"

    # Create staging branch from base
    r = subprocess.run(
        ["git", "-C", target, "checkout", "-b", staging, base_branch],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        # Branch may already exist — check it out instead
        subprocess.run(
            ["git", "-C", target, "checkout", staging],
            capture_output=True,
        )

    # Merge each agent branch in order
    accepted = []
    for jid in chain:
        branch = f"agentic/{jid}"
        r = subprocess.run(
            ["git", "-C", target, "merge", branch, "--no-ff",
             "-m", f"Agent job: {jid}"],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            raise RuntimeError(
                f"Merge conflict on {branch}: {r.stderr.strip()}\n"
                f"Resolve manually in {target} on branch {staging}"
            )
        wt = WORKTREES_DIR / jid
        if wt.exists():
            subprocess.run(
                ["git", "-C", target, "worktree", "remove", str(wt), "--force"],
                capture_output=True,
            )
        accepted.append(jid)

    return {"accepted": accepted, "staging_branch": staging, "target": target}


def abandon_job(job_id: str) -> None:
    """Move a stuck running job to abandoned/ so it can be retried or rejected."""
    _, f = find_job(job_id, states=["running"])
    data = json.loads(f.read_text())
    now  = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    data.setdefault("state_history", []).append({"state": "abandoned", "at": now})
    data["summary"] = "abandoned via dashboard (worker did not complete)"
    (QUEUE_DIR / "abandoned" / f.name).write_text(json.dumps(data, indent=2))
    f.unlink()


def set_chain(job_id: str, parent_id: str | None) -> None:
    """Set or clear a job's parent_request_id (chain position)."""
    if parent_id == job_id:
        raise ValueError("A job cannot be its own parent")
    _, f = find_job(job_id)
    data = json.loads(f.read_text())
    data["parent_request_id"] = parent_id or None
    f.write_text(json.dumps(data, indent=2))


def set_job_status(job_id: str, new_status: str) -> None:
    """Manually move a job to any state, appending a manual transition to state_history."""
    if new_status not in STATES:
        raise ValueError(f"Invalid status: {new_status}")
    job, f = find_job(job_id)
    if job["_state"] == new_status:
        return
    data = json.loads(f.read_text())
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    data.setdefault("state_history", []).append({"state": new_status, "at": now, "manual": True})
    if new_status == "pending":
        data["summary"] = None  # clear summary on retry
    (QUEUE_DIR / new_status / f.name).write_text(json.dumps(data, indent=2))
    f.unlink()


def reject_job(job_id: str) -> None:
    """Remove worktree and delete branch without merging."""
    job, _ = find_job(job_id, states=["done", "failed"])
    target  = job["target_repo"]
    branch  = f"agentic/{job_id}"
    wt      = WORKTREES_DIR / job_id

    if wt.exists():
        subprocess.run(
            ["git", "-C", target, "worktree", "remove", str(wt), "--force"],
            capture_output=True,
        )
    subprocess.run(
        ["git", "-C", target, "branch", "-D", branch],
        capture_output=True,
    )


def get_diff(job_id: str) -> str:
    wt = WORKTREES_DIR / job_id
    if not wt.exists():
        raise ValueError(f"Worktree not found for job {job_id}")
    r = subprocess.run(
        ["git", "-C", str(wt), "diff", "HEAD~1"],
        capture_output=True, text=True,
    )
    return r.stdout


def get_agent_activity(job_id: str) -> dict:
    """Parse agent JSONL log into rich activity data."""
    log_file = WORKTREES_DIR / job_id / ".agent_log.jsonl"
    if not log_file.exists():
        return {"available": False}

    files_read: set[str] = set()
    files_modified: set[str] = set()
    tool_calls: list[dict] = []
    assistant_parts: list[str] = []
    pending: dict[str, dict] = {}
    input_tokens = output_tokens = 0

    try:
        lines = log_file.read_text(errors="replace").splitlines()
    except Exception:
        return {"available": True, "error": "Could not read log"}

    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            ev = json.loads(raw)
        except Exception:
            continue

        t = ev.get("type", "")

        if t == "assistant":
            for block in ev.get("message", {}).get("content", []):
                if block.get("type") == "text":
                    txt = block.get("text", "").strip()
                    if txt:
                        assistant_parts.append(txt)
                elif block.get("type") == "tool_use":
                    pending[block.get("id", "")] = block

        elif t == "tool_use":
            pending[ev.get("id", "")] = ev
            name = ev.get("name", "")
            inp  = ev.get("input", {})
            path = inp.get("file_path") or inp.get("path", "")
            if name == "Read" and path:
                files_read.add(str(path))
            elif name in ("Edit", "Write") and path:
                files_modified.add(str(path))

        elif t == "tool_result":
            uid = ev.get("tool_use_id", "")
            tu  = pending.pop(uid, None)
            if tu:
                name = tu.get("name", "")
                inp  = tu.get("input", {})
                is_error = ev.get("is_error", False)
                content  = ev.get("content", "")
                if isinstance(content, list):
                    content = "\n".join(
                        c.get("text", "") for c in content if isinstance(c, dict)
                    )
                tool_calls.append({
                    "name":    name,
                    "input":   inp,
                    "output":  str(content)[:1000],
                    "success": not is_error,
                })

        elif t == "result":
            usage = ev.get("usage", {})
            input_tokens  += usage.get("input_tokens", 0)
            output_tokens += usage.get("output_tokens", 0)

    # Detect key command outcomes
    build_result = lint_result = None
    for tc in tool_calls:
        if tc["name"] != "Bash":
            continue
        cmd = (tc["input"].get("command") or "").strip()
        if any(x in cmd for x in ("npm run build", "vite build", "tsc")):
            build_result = "passed" if tc["success"] else "failed"
        elif any(x in cmd for x in ("npm run lint", "eslint", "prettier")):
            lint_result = "passed" if tc["success"] else "failed"

    return {
        "available":      True,
        "files_modified": sorted(files_modified),
        "files_read":     sorted(files_read - files_modified),
        "tool_calls":     tool_calls,
        "assistant_text": "\n\n".join(assistant_parts),
        "build_result":   build_result,
        "lint_result":    lint_result,
        "input_tokens":   input_tokens,
        "output_tokens":  output_tokens,
        "total_tokens":   input_tokens + output_tokens,
    }


def read_jobs() -> list:
    jobs = []
    for state in STATES:
        d = QUEUE_DIR / state
        if not d.is_dir():
            continue
        for f in d.glob("*.json"):
            try:
                data = json.loads(f.read_text())
                data["_state"] = state
                jobs.append(data)
            except Exception:
                pass
    jobs.sort(key=lambda j: j.get("submitted_at", ""), reverse=True)
    return jobs


FALLBACK_MODELS = [
    "auto",
    "claude-opus-4-7",
    "claude-opus-4-5",
    "claude-sonnet-4-6",
    "claude-sonnet-4-5",
    "claude-haiku-4-5-20251001",
]

_models_cache: list[str] | None = None

def fetch_models() -> list[str]:
    """Return available Anthropic models, fetched live or from cache. Falls back to static list."""
    global _models_cache
    if _models_cache is not None:
        return _models_cache

    api_key  = os.environ.get("ANTHROPIC_API_KEY", "")
    base_url = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/")

    if not api_key or not base_url.startswith("https://api.anthropic.com"):
        _models_cache = FALLBACK_MODELS
        return _models_cache

    try:
        req = urllib.request.Request(
            f"{base_url}/v1/models",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        ids = ["auto"] + sorted(
            [m["id"] for m in data.get("data", []) if "claude" in m.get("id", "")],
            reverse=True,
        )
        _models_cache = ids if ids else FALLBACK_MODELS
    except Exception:
        _models_cache = FALLBACK_MODELS

    return _models_cache


def get_job_detail(job_id: str) -> dict:
    """Return full job data plus a 'session' key with parsed session artifacts."""
    data, _ = find_job(job_id)  # raises ValueError if not found

    session_dir = AGENTIC_HOME / "worktrees" / job_id / ".claude" / "sessions" / f"queued_{job_id}"
    if not session_dir.exists():
        data["session"] = None
        return data

    session: dict = {}

    # tasks.json
    tasks_file = session_dir / "tasks.json"
    if tasks_file.exists():
        try:
            session["tasks"] = json.loads(tasks_file.read_text())
        except Exception:
            session["tasks"] = None
    else:
        session["tasks"] = None

    # outputs/task_*.txt (exclude _raw.txt and _usage.json)
    outputs: dict = {}
    outputs_dir = session_dir / "outputs"
    if outputs_dir.exists():
        for f in sorted(outputs_dir.glob("task_*.txt")):
            if f.name.endswith("_raw.txt"):
                continue
            stem = f.stem  # e.g. "task_001"
            outputs[stem] = f.read_text()
    session["outputs"] = outputs

    # usage: *_usage.json from session root and outputs/
    usage: dict = {}
    for f in sorted(session_dir.glob("*_usage.json")):
        usage[f.stem] = json.loads(f.read_text())
    if outputs_dir.exists():
        for f in sorted(outputs_dir.glob("*_usage.json")):
            usage[f.stem] = json.loads(f.read_text())
    session["usage"] = usage

    # optional text files
    for key, filename in (
        ("validation_issues",   "validation_issues.txt"),
        ("validation_warnings", "validation_warnings.txt"),
        ("review",              "review.txt"),
    ):
        p = session_dir / filename
        session[key] = p.read_text() if p.exists() else None

    data["session"] = session
    return data

# ── Worker state (global, guarded by _worker_lock) ────────────────────────────

_worker_lock    = threading.Lock()
_worker_running = False
_worker_proc: "subprocess.Popen | None" = None

# ── HTML template ──────────────────────────────────────────────────────────────

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>agentic queue</title>
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
body {
  background: #0d1117; color: #e6edf3;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
  font-size: 14px; line-height: 1.5; min-height: 100vh;
}

/* Header */
header {
  background: #161b22; border-bottom: 1px solid #30363d;
  padding: 12px 24px; display: flex; align-items: center; gap: 10px;
  position: sticky; top: 0; z-index: 100;
}
header h1 { font-size: 16px; font-weight: 600; }
#age { font-size: 12px; color: #8b949e; margin-left: auto; }
.dot { width: 8px; height: 8px; border-radius: 50%; background: #3fb950; flex-shrink: 0; }
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.35} }
.pulse { animation: pulse 1.8s ease-in-out infinite; }

/* Worker button */
#run-btn {
  padding: 6px 14px; border-radius: 6px; border: none; cursor: pointer;
  font-size: 13px; font-weight: 500; background: #238636; color: #fff;
  margin-left: 12px; transition: opacity .15s;
}
#run-btn:hover { opacity: .85; }
#run-btn:disabled { background: #21262d; color: #8b949e; cursor: default; opacity: 1; }

/* Layout */
.container { max-width: 920px; margin: 0 auto; padding: 24px 16px 320px; }

/* Card */
.card {
  background: #161b22; border: 1px solid #30363d; border-radius: 8px;
  padding: 16px; margin-bottom: 16px;
}
.card h2 { font-size: 13px; font-weight: 600; margin-bottom: 12px; color: #8b949e; text-transform: uppercase; letter-spacing: .05em; }

/* Form */
.form-row { display: flex; gap: 8px; margin-top: 8px; flex-wrap: wrap; }
textarea, input[type=text], select {
  background: #0d1117; border: 1px solid #30363d; border-radius: 6px;
  color: #e6edf3; padding: 8px 10px; font-size: 13px; font-family: inherit;
  outline: none; transition: border-color .15s;
}
textarea:focus, input[type=text]:focus, select:focus { border-color: #388bfd; }
textarea { width: 100%; resize: vertical; min-height: 72px; }
input[type=text] { flex: 1; min-width: 200px; }

/* Buttons */
.btn { padding: 7px 14px; border: none; border-radius: 6px; cursor: pointer; font-size: 12px; font-weight: 500; transition: opacity .15s; }
.btn:hover { opacity: .82; }
.btn-green  { background: #238636; color: #fff; }
.btn-blue   { background: #1f4391; color: #88b4ff; border: 1px solid #1f4391; }
.btn-red    { background: #3d1515; color: #f47067; border: 1px solid #6d2120; }
.btn-ghost  { background: transparent; color: #8b949e; border: 1px solid #30363d; }
.btn-ghost:hover { border-color: #8b949e; color: #e6edf3; }

/* Filters */
.filters { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 16px; }
.filter-btn {
  padding: 5px 12px; border-radius: 20px; border: 1px solid #30363d;
  background: transparent; color: #8b949e; cursor: pointer; font-size: 12px; font-weight: 500;
}
.filter-btn:hover { border-color: #8b949e; color: #e6edf3; }
.filter-btn.active { background: #21262d; border-color: #8b949e; color: #e6edf3; }
.badge { display: inline-block; padding: 1px 6px; border-radius: 10px; font-size: 11px; font-weight: 600; background: #21262d; color: #8b949e; margin-left: 4px; }

/* Job cards */
#job-list { display: flex; flex-direction: column; gap: 10px; }
.job-card {
  background: #161b22; border: 1px solid #30363d; border-radius: 8px;
  padding: 14px 16px; display: flex; align-items: flex-start; gap: 12px;
  transition: border-color .15s;
}
.job-card:hover { border-color: #484f58; }
.state-dot { width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; margin-top: 5px; }
.dot-pending   { background: #388bfd; }
.dot-running   { background: #f0883e; animation: pulse 1s ease-in-out infinite; }
.dot-done      { background: #3fb950; }
.dot-failed    { background: #f85149; }
.dot-abandoned { background: #d29922; }
.dot-cancelled { background: #6e7681; }
.job-body { flex: 1; min-width: 0; cursor: pointer; }
.job-top { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 4px; }
.job-id { font-family: "SFMono-Regular", Consolas, monospace; font-size: 12px; color: #8b949e; }
a.job-id { color: #8b949e; text-decoration: none; }
a.job-id:hover { color: #388bfd; text-decoration: underline; }
.state-badge { font-size: 11px; font-weight: 600; padding: 2px 8px; border-radius: 12px; border: 1px solid transparent; }
.badge-pending   { color: #388bfd; border-color: #1f4391; background: #0d1f42; }
.badge-running   { color: #f0883e; border-color: #6d3d1a; background: #2a1900; }
.badge-done      { color: #3fb950; border-color: #1e4a26; background: #0a2614; }
.badge-failed    { color: #f85149; border-color: #6d2120; background: #2c0b0b; }
.badge-abandoned { color: #d29922; border-color: #5a3e1b; background: #2a1f00; }
.badge-cancelled { color: #6e7681; border-color: #30363d; background: #161b22; }
.job-request { font-size: 13px; color: #e6edf3; margin-bottom: 6px; word-break: break-word; }
.job-meta { font-size: 12px; color: #8b949e; display: flex; flex-wrap: wrap; gap: 10px; }
.job-summary { font-size: 12px; color: #3fb950; margin-top: 4px; font-family: "SFMono-Regular", Consolas, monospace; }
.job-summary.failed { color: #f85149; }
.job-actions { flex-shrink: 0; display: flex; gap: 6px; flex-direction: column; align-items: flex-end; }
.empty { text-align: center; padding: 48px 24px; color: #8b949e; }
.empty p { margin-top: 8px; font-size: 13px; }

/* Chain connectors */
.chain-child { margin-left: 24px; position: relative; }
.chain-child::before { content: ''; position: absolute; left: -16px; top: 0; bottom: 50%; border-left: 2px solid #30363d; border-bottom: 2px solid #30363d; width: 14px; border-radius: 0 0 0 4px; }

/* Status popover menu */
.status-menu { position: relative; display: inline-block; }
.status-menu-btn { background: transparent; border: 1px solid #30363d; border-radius: 4px; color: #8b949e; cursor: pointer; padding: 3px 7px; font-size: 12px; }
.status-menu-btn:hover { border-color: #8b949e; color: #e6edf3; }
.status-dropdown { display: none; position: absolute; right: 0; top: 100%; margin-top: 4px; background: #161b22; border: 1px solid #30363d; border-radius: 6px; min-width: 140px; z-index: 50; box-shadow: 0 8px 24px rgba(0,0,0,.4); }
.status-dropdown.open { display: block; }
.status-dropdown button { display: block; width: 100%; text-align: left; padding: 8px 12px; background: none; border: none; color: #e6edf3; cursor: pointer; font-size: 13px; }
.status-dropdown button:hover { background: #21262d; }
.status-dropdown .divider { border-top: 1px solid #21262d; margin: 4px 0; }

/* Log panel — slides up from bottom */
#log-panel {
  position: fixed; bottom: 0; left: 0; right: 0; height: 300px;
  background: #0d1117; border-top: 2px solid #30363d;
  transform: translateY(100%); transition: transform .25s ease;
  z-index: 200; display: flex; flex-direction: column;
}
#log-panel.open { transform: translateY(0); }
#log-header {
  display: flex; align-items: center; gap: 8px;
  padding: 8px 16px; border-bottom: 1px solid #21262d; flex-shrink: 0;
}
#log-header span { font-size: 13px; font-weight: 600; }
#log-close { margin-left: auto; background: none; border: none; color: #8b949e; cursor: pointer; font-size: 18px; line-height: 1; padding: 0 4px; }
#log-close:hover { color: #e6edf3; }
#log-body {
  flex: 1; overflow-y: auto; padding: 12px 16px;
  font-family: "SFMono-Regular", Consolas, monospace; font-size: 12px; line-height: 1.6;
}
.log-line { color: #8b949e; white-space: pre-wrap; word-break: break-all; }
.log-line.success { color: #3fb950; }
.log-line.error   { color: #f85149; }

/* Diff modal */
#diff-modal {
  position: fixed; inset: 0; background: rgba(0,0,0,.75);
  z-index: 300; display: none; align-items: flex-start; justify-content: center;
  padding: 40px 16px; overflow-y: auto;
}
#diff-modal.open { display: flex; }
#diff-box {
  background: #161b22; border: 1px solid #30363d; border-radius: 10px;
  width: 100%; max-width: 860px; display: flex; flex-direction: column;
  max-height: calc(100vh - 80px);
}
#diff-box-header {
  display: flex; align-items: center; gap: 8px;
  padding: 12px 16px; border-bottom: 1px solid #30363d; flex-shrink: 0;
}
#diff-box-header span { font-size: 14px; font-weight: 600; font-family: "SFMono-Regular", Consolas, monospace; }
#diff-close { margin-left: auto; background: none; border: none; color: #8b949e; cursor: pointer; font-size: 20px; padding: 0 4px; }
#diff-close:hover { color: #e6edf3; }
#diff-content {
  overflow-y: auto; padding: 12px;
  font-family: "SFMono-Regular", Consolas, monospace; font-size: 12px; line-height: 1.6;
}
.diff-add    { color: #3fb950; background: #0a2614; display: block; }
.diff-remove { color: #f85149; background: #2c0b0b; display: block; }
.diff-hunk   { color: #388bfd; display: block; }
.diff-meta   { color: #8b949e; display: block; }

/* Chain editor modal */
#chain-modal {
  position: fixed; inset: 0; background: rgba(0,0,0,.75);
  z-index: 400; display: none; align-items: center; justify-content: center;
}
#chain-modal.open { display: flex; }
#chain-box {
  background: #161b22; border: 1px solid #30363d; border-radius: 10px;
  width: 480px; padding: 20px; display: flex; flex-direction: column; gap: 14px;
}
#chain-box h3 { font-size: 14px; font-weight: 600; }
.chain-current { font-size: 12px; color: #8b949e; }
.chain-current span { font-family: monospace; color: #e6edf3; }
#chain-select {
  width: 100%; background: #0d1117; border: 1px solid #30363d; border-radius: 6px;
  color: #e6edf3; padding: 8px 10px; font-size: 13px; font-family: inherit;
}
#chain-select:focus { outline: none; border-color: #388bfd; }
.chain-actions { display: flex; gap: 8px; justify-content: flex-end; }

/* Toast */
#toast {
  position: fixed; bottom: 320px; right: 24px;
  background: #161b22; border: 1px solid #30363d;
  border-radius: 8px; padding: 12px 16px;
  color: #e6edf3; font-size: 13px;
  box-shadow: 0 8px 24px rgba(0,0,0,.4);
  opacity: 0; pointer-events: none; transition: opacity .2s;
  z-index: 999; max-width: 320px;
}
#toast.show { opacity: 1; }
#toast.success { border-color: #238636; }
#toast.error   { border-color: #da3633; }

/* Detail drawer */
#detail-drawer {
  position: fixed;
  top: 56px;
  right: 0;
  bottom: 0;
  width: 58%;
  background: #0d1117;
  border-left: 2px solid #30363d;
  transform: translateX(100%);
  transition: transform .25s ease;
  z-index: 150;
  display: flex;
  flex-direction: column;
  box-shadow: none;
}
#detail-drawer.open {
  transform: translateX(0);
  box-shadow: -8px 0 32px rgba(0,0,0,.5);
}
/* When log panel is also open, stop drawer above it */
#detail-drawer.log-visible { bottom: 300px; }
#drawer-header {
  display: flex; align-items: center; gap: 10px;
  padding: 10px 16px; border-bottom: 1px solid #30363d;
  background: #161b22; flex-shrink: 0;
}
#drawer-close {
  background: none; border: none; color: #8b949e;
  cursor: pointer; font-size: 16px; padding: 2px 6px;
  border-radius: 4px; line-height: 1;
}
#drawer-close:hover { background: #21262d; color: #e6edf3; }
#drawer-job-id { font-family: "SFMono-Regular", Consolas, monospace; font-size: 12px; color: #8b949e; }
#detail-iframe { flex: 1; border: none; background: #0d1117; }
#drawer-backdrop {
  position: fixed; inset: 0;
  background: rgba(0,0,0,.3);
  z-index: 149; display: none;
}
#drawer-backdrop.open { display: block; }
</style>
</head>
<body>

<header>
  <div class="dot pulse"></div>
  <h1>agentic</h1>
  <span style="font-size:12px;color:#6e7681;font-family:monospace">__DEFAULT_REPO__</span>
  <button id="run-btn" onclick="runWorker(false)">▶ Run Worker</button>
  <button id="run-all-btn" onclick="runWorker(true)" style="background:#1f4391;color:#88b4ff;padding:6px 14px;border-radius:6px;border:none;cursor:pointer;font-size:13px;font-weight:500;margin-left:4px;">▶▶ Run All</button>
  <button id="stop-btn" onclick="stopWorker()" style="display:none;background:#6d2120;color:#f47067;padding:6px 14px;border-radius:6px;border:1px solid #6d2120;cursor:pointer;font-size:13px;font-weight:500;margin-left:4px;">■ Stop</button>
  <span id="age">loading…</span>
</header>

<div class="container">
  <div class="card">
    <h2>Submit Job</h2>
    <div style="font-size:12px;color:#6e7681;margin-bottom:8px;font-family:monospace">📁 __DEFAULT_REPO__</div>
    <textarea id="req" placeholder="Describe the change you want…" rows="3"></textarea>
    <div class="form-row">
      <select id="priority">
        <option value="0">Priority 0 — normal</option>
        <option value="1">Priority 1 — high</option>
        <option value="5">Priority 5 — urgent</option>
      </select>
      <select id="model"><option value="auto">auto</option></select>
      <button class="btn btn-green" onclick="submitJob()">Submit</button>
    </div>
    <div class="form-row" style="margin-top:6px">
      <input type="text" id="after" placeholder="Chain after job ID (optional — leave blank for independent)" style="font-family:monospace;font-size:12px">
    </div>
    <div style="font-size:11px;color:#6e7681;margin-top:4px">Cmd+Enter / Ctrl+Enter to submit · Fill <em>Chain after</em> to base this job on a previous job's branch</div>
  </div>

  <div class="filters">
    <button class="filter-btn active" data-state="all"       onclick="setFilter('all',this)">All       <span id="cnt-all"       class="badge">0</span></button>
    <button class="filter-btn"        data-state="pending"   onclick="setFilter('pending',this)">Pending   <span id="cnt-pending"   class="badge">0</span></button>
    <button class="filter-btn"        data-state="running"   onclick="setFilter('running',this)">Running   <span id="cnt-running"   class="badge">0</span></button>
    <button class="filter-btn"        data-state="done"      onclick="setFilter('done',this)">Done      <span id="cnt-done"      class="badge">0</span></button>
    <button class="filter-btn"        data-state="failed"    onclick="setFilter('failed',this)">Failed    <span id="cnt-failed"    class="badge">0</span></button>
    <button class="filter-btn"        data-state="abandoned" onclick="setFilter('abandoned',this)">Abandoned <span id="cnt-abandoned" class="badge">0</span></button>
    <button class="filter-btn"        data-state="cancelled" onclick="setFilter('cancelled',this)">Cancelled <span id="cnt-cancelled" class="badge">0</span></button>
  </div>

  <div id="job-list"></div>
</div>

<!-- Detail drawer — slides in from right, iframe loads /job/<id> -->
<div id="detail-drawer">
  <div id="drawer-header">
    <button id="drawer-close" onclick="closeDrawer()">✕</button>
    <span id="drawer-job-id"></span>
    <a id="drawer-open-tab" href="#" target="_blank" style="margin-left:auto;font-size:12px;color:#8b949e;text-decoration:none;">Open in tab ↗</a>
  </div>
  <iframe id="detail-iframe" src="" frameborder="0" allowfullscreen></iframe>
</div>
<div id="drawer-backdrop" onclick="closeDrawer()"></div>

<!-- Log panel -->
<div id="log-panel">
  <div id="log-header">
    <div class="dot pulse" id="log-dot" style="background:#f0883e"></div>
    <span id="log-title">Worker output</span>
    <button id="log-close" onclick="closeLog()">×</button>
  </div>
  <div id="log-body"></div>
</div>

<!-- Chain editor modal -->
<div id="chain-modal">
  <div id="chain-box">
    <h3>Edit chain position</h3>
    <div class="chain-current" id="chain-current-label">Current parent: <span>none</span></div>
    <select id="chain-select">
      <option value="">— run independently (no parent) —</option>
    </select>
    <div style="font-size:11px;color:#6e7681">
      Choose which job this one should run <em>after</em>. Jobs will execute in chain order when you click ▶▶ Run All.
    </div>
    <div class="chain-actions">
      <button class="btn btn-ghost" onclick="closeChain()">Cancel</button>
      <button class="btn btn-red"   onclick="clearChain()">Remove from chain</button>
      <button class="btn btn-green" onclick="saveChain()">Save</button>
    </div>
  </div>
</div>

<!-- Diff modal -->
<div id="diff-modal" onclick="closeDiff(event)">
  <div id="diff-box">
    <div id="diff-box-header">
      <span id="diff-title">diff</span>
      <button id="diff-close" onclick="closeDiff()">×</button>
    </div>
    <div id="diff-content"></div>
  </div>
</div>

<div id="toast"></div>

<script>
let currentFilter = 'all';
let allJobs = [];
let lastFetch = null;

function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

function relTime(iso) {
  if (!iso) return '';
  const d = Math.floor((Date.now() - new Date(iso)) / 1000);
  if (d < 60)    return d + 's ago';
  if (d < 3600)  return Math.floor(d/60) + 'm ago';
  if (d < 86400) return Math.floor(d/3600) + 'h ago';
  return Math.floor(d/86400) + 'd ago';
}

function setFilter(state, btn) {
  currentFilter = state;
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  renderJobs();
}

function buildJobCard(j) {
  const s = j._state || 'pending';
  const sumCls = (s === 'failed') ? 'job-summary failed' : 'job-summary';
  const actions = [];
  const sp = "event.stopPropagation();";
  if (s === 'pending')
    actions.push(`<button class="btn btn-ghost" onclick="${sp}cancelJob('${escHtml(j.id)}')">Cancel</button>`);
  if (s === 'running')
    actions.push(`<button class="btn btn-red" onclick="${sp}abandonJob('${escHtml(j.id)}')">Abandon</button>`);
  if (s === 'done' || s === 'failed')
    actions.push(`<button class="btn btn-ghost" onclick="${sp}viewDiff('${escHtml(j.id)}')">View Diff</button>`);
  if (s === 'done') {
    const hasDoneChild = allJobs.some(x => x.parent_request_id === j.id && x._state === 'done');
    if (hasDoneChild)
      actions.push(`<button class="btn btn-blue" onclick="${sp}acceptChain('${escHtml(j.id)}')">Accept Chain ↓</button>`);
    actions.push(`<button class="btn btn-blue" style="opacity:.7" onclick="${sp}acceptJob('${escHtml(j.id)}')">Accept</button>`);
  }
  if (s === 'done' || s === 'failed')
    actions.push(`<button class="btn btn-red" onclick="${sp}rejectJob('${escHtml(j.id)}')">Reject</button>`);

  // Status + chain menu — always present
  const allStatuses = ['pending','running','done','failed','abandoned','cancelled'];
  const statusItems = allStatuses.filter(x => x !== s).map(st =>
    `<button onclick="event.stopPropagation();setStatus('${escHtml(j.id)}','${st}')">→ ${st}</button>`
  ).join('');
  const chainLabel = j.parent_request_id ? '🔗 Edit chain…' : '🔗 Set chain…';
  const menuItems = `<button onclick="event.stopPropagation();openChain('${escHtml(j.id)}')">${chainLabel}</button><div class="divider"></div>${statusItems}`;
  actions.push(`<div class="status-menu">
    <button class="status-menu-btn" onclick="event.stopPropagation();toggleMenu(this)">⋯</button>
    <div class="status-dropdown">${menuItems}</div>
  </div>`);

  return `
<div class="job-card">
  <div class="state-dot dot-${escHtml(s)}"></div>
  <div class="job-body" onclick="openDrawer('${escHtml(j.id)}')">
    <div class="job-top">
      <a class="job-id" href="/job/${escHtml(j.id)}">${escHtml(j.id)}</a>
      <span class="state-badge badge-${escHtml(s)}">${escHtml(s)}</span>
    </div>
    <div class="job-request">${escHtml(j.request || '')}</div>
    <div class="job-meta">
      <span>⏱ ${relTime(j.submitted_at)}</span>
      <span>📁 ${escHtml(j.target_repo || '')}</span>
      ${j.model_hint ? '<span>🤖 ' + escHtml(j.model_hint) + '</span>' : ''}
      ${j.priority   ? '<span>⬆ p' + escHtml(String(j.priority)) + '</span>' : ''}
    </div>
    ${j.summary ? `<div class="${sumCls}">↳ ${escHtml(j.summary)}</div>` : ''}
  </div>
  <div class="job-actions">${actions.join('')}</div>
</div>`;
}

function renderJobs() {
  const list = document.getElementById('job-list');
  const jobs = currentFilter === 'all' ? allJobs : allJobs.filter(j => j._state === currentFilter);
  if (jobs.length === 0) {
    list.innerHTML = '<div class="empty"><div style="font-size:32px">📭</div><p>No jobs' +
      (currentFilter !== 'all' ? ' in <strong>' + escHtml(currentFilter) + '</strong>' : '') + '</p></div>';
    return;
  }

  // Build lookup structures for chain rendering
  const jobById = {};
  jobs.forEach(j => { jobById[j.id] = j; });
  const childMap = {};
  jobs.forEach(j => {
    if (j.parent_request_id) {
      childMap[j.parent_request_id] = childMap[j.parent_request_id] || [];
      childMap[j.parent_request_id].push(j.id);
    }
  });

  function renderChain(job, depth) {
    const children = (childMap[job.id] || []).map(cid => jobById[cid]).filter(Boolean);
    const cardHtml = buildJobCard(job);
    const childrenHtml = children.map(c => `<div class="chain-child">${renderChain(c, depth + 1)}</div>`).join('');
    return cardHtml + childrenHtml;
  }

  // Identify root jobs: no parent, or parent not in current filtered list
  const rootJobs = jobs.filter(j => !j.parent_request_id || !jobById[j.parent_request_id]);
  list.innerHTML = rootJobs.map(j => renderChain(j, 0)).join('');
}

function updateCounts(jobs) {
  const c = {all:0,pending:0,running:0,done:0,failed:0,abandoned:0,cancelled:0};
  jobs.forEach(j => { c.all++; if (c[j._state] !== undefined) c[j._state]++; });
  Object.keys(c).forEach(k => { const el = document.getElementById('cnt-'+k); if(el) el.textContent = c[k]; });
}

async function fetchJobs() {
  try {
    const r = await fetch('/api/jobs');
    if (!r.ok) return;
    allJobs = await r.json();
    lastFetch = Date.now();
    updateCounts(allJobs);
    renderJobs();
  } catch(e) {}
}

async function submitJob() {
  const request    = document.getElementById('req').value.trim();
  const repo       = '__DEFAULT_REPO__';
  const priority   = parseInt(document.getElementById('priority').value, 10);
  const model_hint = document.getElementById('model').value;
  const after      = document.getElementById('after').value.trim();
  if (!request) { toast('Request cannot be empty', 'error'); return; }
  try {
    const r = await fetch('/api/submit', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({request, repo, priority, model_hint, after}),
    });
    const d = await r.json();
    if (d.ok) { document.getElementById('req').value = ''; toast('Submitted ' + d.id, 'success'); fetchJobs(); }
    else toast('Error: ' + (d.error || 'unknown'), 'error');
  } catch(e) { toast('Network error', 'error'); }
}

async function cancelJob(id) {
  const r = await fetch('/api/cancel', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})});
  const d = await r.json();
  if (d.ok) { toast('Cancelled ' + id, 'success'); fetchJobs(); }
  else toast('Error: ' + (d.error || 'unknown'), 'error');
}

function toggleMenu(btn) {
  const dd = btn.nextElementSibling;
  document.querySelectorAll('.status-dropdown.open').forEach(d => { if (d !== dd) d.classList.remove('open'); });
  dd.classList.toggle('open');
}
document.addEventListener('click', () => {
  document.querySelectorAll('.status-dropdown.open').forEach(d => d.classList.remove('open'));
});

/* ── Chain editor ── */
let _chainJobId = null;

function openChain(id) {
  _chainJobId = id;
  const job = allJobs.find(j => j.id === id);
  const current = job && job.parent_request_id;
  document.getElementById('chain-current-label').innerHTML =
    'Current parent: <span>' + (current ? escHtml(current) : 'none (independent)') + '</span>';

  // Populate dropdown — all jobs except this one and its own descendants
  const sel = document.getElementById('chain-select');
  sel.innerHTML = '<option value="">— run independently (no parent) —</option>';
  allJobs.filter(j => j.id !== id).forEach(j => {
    const opt = document.createElement('option');
    opt.value = j.id;
    opt.textContent = j.id + '  [' + j._state + ']  ' + j.request.slice(0, 50);
    opt.selected = j.id === current;
    sel.appendChild(opt);
  });

  document.getElementById('chain-modal').classList.add('open');
}

function closeChain() {
  document.getElementById('chain-modal').classList.remove('open');
  _chainJobId = null;
}

async function saveChain() {
  const parentId = document.getElementById('chain-select').value || null;
  const r = await fetch('/api/set-chain', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:_chainJobId, parent_id:parentId})});
  const d = await r.json();
  if (d.ok) { toast(parentId ? 'Chained after ' + parentId : 'Removed from chain', 'success'); closeChain(); fetchJobs(); }
  else toast('Error: ' + (d.error || 'unknown'), 'error');
}

async function clearChain() {
  const r = await fetch('/api/set-chain', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:_chainJobId, parent_id:null})});
  const d = await r.json();
  if (d.ok) { toast('Removed from chain', 'success'); closeChain(); fetchJobs(); }
  else toast('Error: ' + (d.error || 'unknown'), 'error');
}

async function setStatus(id, status) {
  const r = await fetch('/api/set-status', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id, status})});
  const d = await r.json();
  if (d.ok) { toast('Moved ' + id + ' → ' + status, 'success'); fetchJobs(); }
  else toast('Error: ' + (d.error || 'unknown'), 'error');
}

async function abandonJob(id) {
  if (!confirm('Mark this running job as failed? (Use this to unstick a job whose worker crashed)')) return;
  const r = await fetch('/api/abandon', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})});
  const d = await r.json();
  if (d.ok) { toast('Abandoned ' + id, 'success'); fetchJobs(); }
  else toast('Error: ' + (d.error || 'unknown'), 'error');
}

async function acceptChain(id) {
  const children = allJobs.filter(j => j.parent_request_id === id && j._state === 'done');
  if (!confirm(`Accept this job and ${children.length} chained job(s) in order? (${children.length + 1} total merges)`)) return;
  const r = await fetch('/api/accept-chain', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})});
  const d = await r.json();
  if (d.ok) { toast(`${d.accepted.length} job(s) merged → ${d.staging_branch}`, 'success'); fetchJobs(); }
  else toast('Error: ' + (d.error || 'unknown'), 'error');
}

async function acceptJob(id) {
  if (!confirm('Merge agentic/' + id + ' into its base branch?')) return;
  const r = await fetch('/api/accept', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})});
  const d = await r.json();
  if (d.ok) { toast('Accepted ' + id + ' → ' + (d.message || ''), 'success'); fetchJobs(); }
  else toast('Accept failed: ' + (d.error || 'unknown'), 'error');
}

async function rejectJob(id) {
  if (!confirm('Discard agentic/' + id + ' and delete the worktree?')) return;
  const r = await fetch('/api/reject', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})});
  const d = await r.json();
  if (d.ok) { toast('Rejected ' + id, 'success'); fetchJobs(); }
  else toast('Reject failed: ' + (d.error || 'unknown'), 'error');
}

/* ── Worker streaming ── */
let workerEs = null;

async function stopWorker() {
  const r = await fetch('/api/stop-worker', {method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
  const d = await r.json();
  if (d.ok) {
    appendLog('■ Stop requested — waiting for worker to exit…', 'error');
  } else {
    toast('Stop failed: ' + (d.error || 'unknown'), 'error');
  }
}
let _runAll   = false;

function runWorker(loop = false) {
  if (workerEs) return;
  _runAll = loop;
  const btn    = document.getElementById('run-btn');
  const allBtn = document.getElementById('run-all-btn');
  const body   = document.getElementById('log-body');
  btn.disabled = true; btn.textContent = '⏳ Running…';
  allBtn.disabled = true;
  document.getElementById('stop-btn').style.display = 'inline-block';
  body.innerHTML = '';
  document.getElementById('log-panel').classList.add('open');
  _syncDrawerWithLog(true);
  document.getElementById('log-dot').style.background = '#f0883e';
  document.getElementById('log-title').textContent = 'Worker running…';

  workerEs = new EventSource('/api/worker-stream');
  workerEs.onmessage = e => {
    const msg = JSON.parse(e.data);
    // Track which job was claimed so we can auto-open its drawer on completion
    if (msg.line && msg.line.startsWith('🔧 Claimed:')) {
      const match = msg.line.match(/j_[0-9a-f_]+/);
      if (match) window._lastClaimedJobId = match[0];
    }
    if (msg.done !== undefined) {
      workerEs.close(); workerEs = null;
      const rc = msg.rc;
      document.getElementById('log-dot').style.background = rc === 0 ? '#3fb950' : '#f85149';
      document.getElementById('log-title').textContent = rc === 0 ? 'Worker done ✓' : 'Worker failed (exit ' + rc + ')';
      fetchJobs().then(() => {
        const pending = allJobs.filter(j => j._state === 'pending').length;
        if (_runAll && rc === 0 && pending > 0) {
          appendLog('', '');
          appendLog('── ' + pending + ' job(s) still pending — starting next worker…', 'success');
          runWorker(true);
        } else {
          _runAll = false;
          btn.disabled = false; btn.textContent = '▶ Run Worker';
          document.getElementById('run-all-btn').disabled = false;
          document.getElementById('stop-btn').style.display = 'none';
          if (_runAll && pending === 0) appendLog('Queue empty — all done.', 'success');
        }
      });
      if (window._lastClaimedJobId && !_runAll) {
        setTimeout(() => openDrawer(window._lastClaimedJobId), 500);
        window._lastClaimedJobId = null;
      }
      return;
    }
    if (msg.error) { appendLog(msg.error, 'error'); return; }
    const line = msg.line || '';
    const cls  = line.startsWith('✅') || line.startsWith('✓') ? 'success'
               : line.startsWith('❌') || line.startsWith('Error') ? 'error' : '';
    appendLog(line, cls);
  };
  workerEs.onerror = () => {
    if (workerEs) { workerEs.close(); workerEs = null; }
    btn.disabled = false; btn.textContent = '▶ Run Worker';
    appendLog('Connection lost', 'error');
  };
}

function appendLog(line, cls) {
  const body = document.getElementById('log-body');
  const el = document.createElement('div');
  el.className = 'log-line' + (cls ? ' ' + cls : '');
  el.textContent = line;
  body.appendChild(el);
  body.scrollTop = body.scrollHeight;
}

function closeLog() {
  document.getElementById('log-panel').classList.remove('open');
  _syncDrawerWithLog(false);
}

/* ── Diff modal ── */
async function viewDiff(id) {
  document.getElementById('diff-title').textContent = 'diff — ' + id;
  document.getElementById('diff-content').innerHTML = '<div style="padding:16px;color:#8b949e">Loading…</div>';
  document.getElementById('diff-modal').classList.add('open');
  try {
    const r = await fetch('/api/diff/' + encodeURIComponent(id));
    const d = await r.json();
    if (!d.ok) { document.getElementById('diff-content').innerHTML = '<div style="padding:16px;color:#f85149">' + escHtml(d.error) + '</div>'; return; }
    const lines = (d.diff || '').split('\n');
    document.getElementById('diff-content').innerHTML = lines.map(l => {
      const cls = l.startsWith('+') && !l.startsWith('+++') ? 'diff-add'
                : l.startsWith('-') && !l.startsWith('---') ? 'diff-remove'
                : l.startsWith('@@') ? 'diff-hunk'
                : l.startsWith('diff ') || l.startsWith('index ') || l.startsWith('---') || l.startsWith('+++') ? 'diff-meta'
                : '';
      return `<span class="${cls}">${escHtml(l)}</span>`;
    }).join('\n');
  } catch(e) {
    document.getElementById('diff-content').innerHTML = '<div style="padding:16px;color:#f85149">Failed to load diff</div>';
  }
}

function closeDiff(e) {
  if (!e || e.target === document.getElementById('diff-modal') || e.currentTarget === document.getElementById('diff-close'))
    document.getElementById('diff-modal').classList.remove('open');
}

/* ── Toast ── */
let toastTimer;
function toast(msg, type) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = 'show ' + (type || '');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = ''; }, 3000);
}

document.addEventListener('keydown', e => {
  if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') submitJob();
  if (e.key === 'Escape') { closeDiff(); closeChain(); }
});
document.getElementById('chain-modal').addEventListener('click', e => {
  if (e.target === document.getElementById('chain-modal')) closeChain();
});

/* ── Detail drawer ── */
function openDrawer(id) {
  const drawer = document.getElementById('detail-drawer');
  const backdrop = document.getElementById('drawer-backdrop');
  const iframe = document.getElementById('detail-iframe');
  const jobIdEl = document.getElementById('drawer-job-id');
  const tabLink = document.getElementById('drawer-open-tab');

  jobIdEl.textContent = id;
  tabLink.href = '/job/' + encodeURIComponent(id);
  iframe.src = '/job/' + encodeURIComponent(id);

  drawer.classList.add('open');
  backdrop.classList.add('open');
  // Adjust bottom if log panel is open
  if (document.getElementById('log-panel').classList.contains('open')) {
    drawer.classList.add('log-visible');
  }
}

function closeDrawer() {
  document.getElementById('detail-drawer').classList.remove('open', 'log-visible');
  document.getElementById('drawer-backdrop').classList.remove('open');
  document.getElementById('detail-iframe').src = '';
}

// Keep drawer bottom in sync with log panel
const _origOpenLog = window._openLog;
function _syncDrawerWithLog(open) {
  const drawer = document.getElementById('detail-drawer');
  if (drawer.classList.contains('open')) {
    if (open) drawer.classList.add('log-visible');
    else drawer.classList.remove('log-visible');
  }
}

// Populate model dropdown from API
fetch('/api/models').then(r => r.json()).then(models => {
  const sel = document.getElementById('model');
  sel.innerHTML = models.map(m =>
    `<option value="${escHtml(m)}">${escHtml(m)}</option>`
  ).join('');
}).catch(() => {});

fetchJobs();
setInterval(fetchJobs, 3000);
setInterval(() => {
  const el = document.getElementById('age');
  if (!lastFetch) return;
  el.textContent = 'updated ' + Math.floor((Date.now() - lastFetch)/1000) + 's ago';
}, 1000);
</script>
</body>
</html>
"""

JOB_DETAIL_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>agentic job detail</title>
<script>const JOB_ID = "__JOB_ID__";</script>
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
body {
  background: #0d1117; color: #e6edf3;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
  font-size: 14px; line-height: 1.5; min-height: 100vh;
}
header {
  background: #161b22; border-bottom: 1px solid #30363d;
  padding: 12px 24px; display: flex; align-items: center; gap: 12px;
  position: sticky; top: 0; z-index: 100; flex-wrap: wrap;
}
header a.back { color: #8b949e; text-decoration: none; font-size: 13px; }
header a.back:hover { color: #388bfd; text-decoration: underline; }
#job-id-title { font-family: "SFMono-Regular", Consolas, monospace; font-size: 14px; font-weight: 600; }
.header-actions { margin-left: auto; display: flex; gap: 6px; flex-wrap: wrap; }
.container { max-width: 920px; margin: 0 auto; padding: 24px 16px 80px; }
.card {
  background: #161b22; border: 1px solid #30363d; border-radius: 8px;
  padding: 16px; margin-bottom: 16px;
}
.card h2 { font-size: 13px; font-weight: 600; margin-bottom: 12px; color: #8b949e; text-transform: uppercase; letter-spacing: .05em; }
.state-badge { font-size: 11px; font-weight: 600; padding: 2px 8px; border-radius: 12px; border: 1px solid transparent; }
.badge-pending   { color: #388bfd; border-color: #1f4391; background: #0d1f42; }
.badge-running   { color: #f0883e; border-color: #6d3d1a; background: #2a1900; }
.badge-done      { color: #3fb950; border-color: #1e4a26; background: #0a2614; }
.badge-failed    { color: #f85149; border-color: #6d2120; background: #2c0b0b; }
.badge-abandoned { color: #d29922; border-color: #5a3e1b; background: #2a1f00; }
.badge-cancelled { color: #6e7681; border-color: #30363d; background: #161b22; }
.btn { padding: 7px 14px; border: none; border-radius: 6px; cursor: pointer; font-size: 12px; font-weight: 500; transition: opacity .15s; }
.btn:hover { opacity: .82; }
.btn-blue   { background: #1f4391; color: #88b4ff; border: 1px solid #1f4391; }
.btn-red    { background: #3d1515; color: #f47067; border: 1px solid #6d2120; }
pre {
  background: #010409; border: 1px solid #21262d; border-radius: 6px;
  padding: 12px; overflow-x: auto; font-size: 12px; line-height: 1.5;
  max-height: 400px; overflow-y: auto;
  font-family: "SFMono-Regular", Consolas, monospace;
  white-space: pre-wrap; word-break: break-word;
}
/* Timeline */
.timeline { display: flex; flex-direction: column; gap: 0; }
.tl-entry { display: flex; align-items: flex-start; gap: 12px; padding: 8px 0; position: relative; }
.tl-entry:not(:last-child)::after {
  content: ''; position: absolute; left: 7px; top: 24px; bottom: -8px;
  width: 2px; background: #30363d;
}
.tl-dot { width: 16px; height: 16px; border-radius: 50%; flex-shrink: 0; margin-top: 2px; border: 2px solid #0d1117; }
.tl-dot-pending   { background: #388bfd; }
.tl-dot-running   { background: #f0883e; }
.tl-dot-done      { background: #3fb950; }
.tl-dot-failed    { background: #f85149; }
.tl-dot-cancelled { background: #6e7681; }
.tl-body { flex: 1; }
.tl-state { font-weight: 600; font-size: 13px; text-transform: capitalize; }
.tl-meta { font-size: 12px; color: #8b949e; margin-top: 1px; }
/* Tasks */
.task-card { border: 1px solid #30363d; border-radius: 6px; padding: 12px; margin-bottom: 10px; }
.task-header { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; flex-wrap: wrap; }
.task-id { font-family: "SFMono-Regular", Consolas, monospace; font-size: 12px; color: #8b949e; }
.action-badge { font-size: 11px; font-weight: 600; padding: 2px 7px; border-radius: 10px; border: 1px solid transparent; }
.action-CREATE { color: #3fb950; border-color: #1e4a26; background: #0a2614; }
.action-MODIFY { color: #388bfd; border-color: #1f4391; background: #0d1f42; }
.action-DELETE { color: #f85149; border-color: #6d2120; background: #2c0b0b; }
.task-file { font-family: "SFMono-Regular", Consolas, monospace; font-size: 12px; color: #e6edf3; }
.task-desc { font-size: 13px; color: #8b949e; margin-top: 4px; }
.task-modtype { font-size: 11px; color: #6e7681; margin-top: 2px; }
details summary { cursor: pointer; color: #8b949e; font-size: 12px; margin-top: 8px; user-select: none; }
details summary:hover { color: #388bfd; }
details[open] summary { margin-bottom: 6px; }
/* Usage table */
table { width: 100%; border-collapse: collapse; font-size: 12px; }
th { text-align: left; padding: 6px 8px; color: #8b949e; border-bottom: 1px solid #30363d; font-weight: 600; }
td { padding: 6px 8px; border-bottom: 1px solid #21262d; font-family: "SFMono-Regular", Consolas, monospace; }
tr:last-child td { border-bottom: none; font-weight: 700; color: #e6edf3; }
tr.totals-row td { border-top: 1px solid #30363d; color: #e6edf3; }
/* Validation */
.validation-issues { background: #2c0b0b; border: 1px solid #6d2120; border-radius: 6px; padding: 12px; margin-bottom: 8px; }
.validation-warnings { background: #2a1900; border: 1px solid #6d3d1a; border-radius: 6px; padding: 12px; }
.validation-label { font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: .05em; margin-bottom: 6px; }
.validation-issues .validation-label { color: #f85149; }
.validation-warnings .validation-label { color: #f0883e; }
.validation-issues pre, .validation-warnings pre { background: transparent; border: none; padding: 0; max-height: none; }
/* Toast */
#toast {
  position: fixed; bottom: 24px; right: 24px;
  background: #161b22; border: 1px solid #30363d;
  border-radius: 8px; padding: 12px 16px;
  color: #e6edf3; font-size: 13px;
  box-shadow: 0 8px 24px rgba(0,0,0,.4);
  opacity: 0; pointer-events: none; transition: opacity .2s;
  z-index: 999; max-width: 320px;
}
#toast.show { opacity: 1; }
#toast.success { border-color: #238636; }
#toast.error   { border-color: #da3633; }
#error-msg { text-align: center; padding: 48px 24px; color: #f85149; }
</style>
</head>
<body>

<header>
  <a class="back" href="/">&#8592; Back</a>
  <span id="job-id-title"></span>
  <span id="state-badge-header"></span>
  <div class="header-actions" id="header-actions"></div>
</header>

<div class="container" id="main-content">
  <div style="padding:48px 0; text-align:center; color:#8b949e;">Loading…</div>
</div>

<div id="toast"></div>

<script>
let toastTimer;
function toast(msg, type) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = 'show ' + (type || '');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = ''; }, 3000);
}

function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

function relTime(iso) {
  if (!iso) return '';
  const d = Math.floor((Date.now() - new Date(iso)) / 1000);
  if (d < 60)    return d + 's ago';
  if (d < 3600)  return Math.floor(d/60) + 'm ago';
  if (d < 86400) return Math.floor(d/3600) + 'h ago';
  return Math.floor(d/86400) + 'd ago';
}

async function acceptJob(id) {
  if (!confirm('Merge agentic/' + id + ' into the target repo\'s current branch?')) return;
  const r = await fetch('/api/accept', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})});
  const d = await r.json();
  if (d.ok) { toast('Accepted ' + id, 'success'); setTimeout(() => { _notifyParent(); }, 1200); }
  else toast('Accept failed: ' + (d.error || 'unknown'), 'error');
}

async function rejectJob(id) {
  if (!confirm('Discard agentic/' + id + ' and delete the worktree?')) return;
  const r = await fetch('/api/reject', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})});
  const d = await r.json();
  if (d.ok) { toast('Rejected ' + id, 'success'); setTimeout(() => { _notifyParent(); }, 1200); }
  else toast('Reject failed: ' + (d.error || 'unknown'), 'error');
}

async function abandonJob(id) {
  if (!confirm('Mark this running job as failed? (Use this to unstick a job whose worker crashed)')) return;
  const r = await fetch('/api/abandon', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})});
  const d = await r.json();
  if (d.ok) { toast('Abandoned ' + id, 'success'); setTimeout(() => { _notifyParent(); }, 1200); }
  else toast('Error: ' + (d.error || 'unknown'), 'error');
}

function renderPage(job) {
  const s = job._state || 'pending';
  document.title = 'agentic — ' + job.id;
  document.getElementById('job-id-title').textContent = job.id;
  document.getElementById('state-badge-header').innerHTML =
    `<span class="state-badge badge-${escHtml(s)}">${escHtml(s)}</span>`;

  // Action buttons
  const actions = [];
  if (s === 'running')
    actions.push(`<button class="btn btn-red" onclick="abandonJob('${escHtml(job.id)}')">Abandon</button>`);
  if (s === 'done')
    actions.push(`<button class="btn btn-blue" onclick="acceptJob('${escHtml(job.id)}')">Accept</button>`);
  if (s === 'done' || s === 'failed')
    actions.push(`<button class="btn btn-red" onclick="rejectJob('${escHtml(job.id)}')">Reject</button>`);
  document.getElementById('header-actions').innerHTML = actions.join('');

  let html = '';

  // ── 1. Request ──
  html += `<div class="card"><h2>Request</h2><pre>${escHtml(job.request || '')}</pre></div>`;

  // ── 2. State History ──
  const hist = job.state_history || [];
  if (hist.length) {
    html += `<div class="card"><h2>State History</h2><div class="timeline">`;
    hist.forEach(entry => {
      const st = entry.state || '';
      html += `<div class="tl-entry">
        <div class="tl-dot tl-dot-${escHtml(st)}"></div>
        <div class="tl-body">
          <div class="tl-state">${escHtml(st)}</div>
          <div class="tl-meta">${escHtml(entry.at || '')}${entry.at ? ' · ' + relTime(entry.at) : ''}${entry.worker ? ' · ' + escHtml(entry.worker) : ''}</div>
        </div>
      </div>`;
    });
    html += `</div></div>`;
  }

  const sess = job.session;

  // ── 3. Tasks ──
  if (sess && sess.tasks && sess.tasks.tasks && sess.tasks.tasks.length) {
    html += `<div class="card"><h2>Tasks</h2>`;
    sess.tasks.tasks.forEach(task => {
      const taskKey = 'task_' + String(task.id).padStart(3, '0');
      const actionCls = 'action-' + (task.action || '').toUpperCase();
      const outputText = sess.outputs && sess.outputs[taskKey];
      const lineCount = outputText ? outputText.split('\n').length : 0;
      html += `<div class="task-card">
        <div class="task-header">
          <span class="task-id">#${escHtml(String(task.id))}</span>
          <span class="action-badge ${escHtml(actionCls)}">${escHtml((task.action || '').toUpperCase())}</span>
          <span class="task-file">${escHtml(task.file_path || task.path || '')}</span>
        </div>
        ${task.description ? `<div class="task-desc">${escHtml(task.description)}</div>` : ''}
        ${task.modification_type ? `<div class="task-modtype">${escHtml(task.modification_type)}</div>` : ''}
        ${outputText ? `<details><summary>View output (${lineCount} lines)</summary><pre>${escHtml(outputText)}</pre></details>` : ''}
      </div>`;
    });
    html += `</div>`;
  }

  // ── 4. Agent Activity ──
  fetch('/api/activity/' + encodeURIComponent(JOB_ID))
    .then(r => r.json())
    .then(act => {
      if (!act.available) return;

      let ahtml = `<div class="card" id="activity-card">
        <h2>Agent Activity</h2>`;

      // ── Stats row ──
      ahtml += `<div style="display:flex;gap:16px;flex-wrap:wrap;margin-bottom:16px;padding:12px;background:#010409;border-radius:6px;border:1px solid #21262d">`;
      ahtml += `<div style="text-align:center"><div style="font-size:20px;font-weight:600;color:#e6edf3">${(act.files_modified||[]).length}</div><div style="font-size:11px;color:#8b949e">files changed</div></div>`;
      ahtml += `<div style="text-align:center"><div style="font-size:20px;font-weight:600;color:#e6edf3">${(act.tool_calls||[]).length}</div><div style="font-size:11px;color:#8b949e">tool calls</div></div>`;
      if (act.build_result) {
        const bc = act.build_result === 'passed' ? '#3fb950' : '#f85149';
        const bi = act.build_result === 'passed' ? '✓' : '✗';
        ahtml += `<div style="text-align:center"><div style="font-size:20px;font-weight:600;color:${bc}">${bi}</div><div style="font-size:11px;color:#8b949e">build</div></div>`;
      }
      if (act.lint_result) {
        const lc = act.lint_result === 'passed' ? '#3fb950' : '#f85149';
        const li = act.lint_result === 'passed' ? '✓' : '✗';
        ahtml += `<div style="text-align:center"><div style="font-size:20px;font-weight:600;color:${lc}">${li}</div><div style="font-size:11px;color:#8b949e">lint</div></div>`;
      }
      if (act.total_tokens) {
        ahtml += `<div style="text-align:center"><div style="font-size:20px;font-weight:600;color:#e6edf3">${(act.total_tokens/1000).toFixed(1)}k</div><div style="font-size:11px;color:#8b949e">tokens</div></div>`;
      }
      ahtml += `</div>`;

      // ── Files modified ──
      if ((act.files_modified||[]).length) {
        ahtml += `<div style="margin-bottom:14px">
          <div style="font-size:11px;font-weight:600;color:#8b949e;text-transform:uppercase;letter-spacing:.05em;margin-bottom:6px">Files Modified</div>`;
        act.files_modified.forEach(f => {
          ahtml += `<div style="display:flex;align-items:center;gap:8px;padding:4px 0;border-bottom:1px solid #21262d">
            <span style="color:#3fb950;font-size:13px">✎</span>
            <span style="font-family:monospace;font-size:12px;color:#e6edf3">${escHtml(f)}</span>
          </div>`;
        });
        ahtml += `</div>`;
      }

      // ── Files read ──
      if ((act.files_read||[]).length) {
        ahtml += `<div style="margin-bottom:14px">
          <div style="font-size:11px;font-weight:600;color:#8b949e;text-transform:uppercase;letter-spacing:.05em;margin-bottom:6px">Files Read</div>
          <div style="display:flex;flex-wrap:wrap;gap:6px">`;
        act.files_read.forEach(f => {
          ahtml += `<span style="font-family:monospace;font-size:11px;color:#6e7681;background:#161b22;border:1px solid #30363d;border-radius:4px;padding:2px 6px">${escHtml(f)}</span>`;
        });
        ahtml += `</div></div>`;
      }

      // ── Commands ──
      const cmds = (act.tool_calls||[]).filter(tc => tc.name === 'Bash');
      if (cmds.length) {
        ahtml += `<div style="margin-bottom:14px">
          <div style="font-size:11px;font-weight:600;color:#8b949e;text-transform:uppercase;letter-spacing:.05em;margin-bottom:6px">Commands Run</div>`;
        cmds.forEach(tc => {
          const cmd = (tc.input.command||'').trim().slice(0, 150);
          const ok  = tc.success;
          ahtml += `<div style="margin-bottom:6px">
            <div style="display:flex;align-items:center;gap:8px;padding:6px 10px;background:#010409;border-radius:4px;border-left:3px solid ${ok?'#3fb950':'#f85149'}">
              <span style="font-size:12px">${ok?'✓':'✗'}</span>
              <code style="font-size:12px;color:#e6edf3;word-break:break-all">$ ${escHtml(cmd)}</code>
            </div>
            ${!ok && tc.output ? `<details style="margin-top:4px"><summary style="font-size:11px;color:#f85149;cursor:pointer;padding-left:10px">Show error output</summary><pre style="margin-top:4px;max-height:200px;overflow-y:auto;font-size:11px">${escHtml(tc.output.slice(0,2000))}</pre></details>` : ''}
          </div>`;
        });
        ahtml += `</div>`;
      }

      // ── Agent reasoning (collapsible) ──
      if (act.assistant_text && act.assistant_text.trim().length > 50) {
        ahtml += `<details>
          <summary style="font-size:12px;color:#8b949e;cursor:pointer;user-select:none;padding:6px 0">
            Agent reasoning (${act.assistant_text.length.toLocaleString()} chars)
          </summary>
          <pre style="margin-top:8px;max-height:400px;overflow-y:auto;font-size:12px;white-space:pre-wrap">${escHtml(act.assistant_text.slice(0,8000))}</pre>
        </details>`;
      }

      // ── Token detail ──
      if (act.input_tokens || act.output_tokens) {
        ahtml += `<div style="margin-top:12px;padding-top:12px;border-top:1px solid #21262d;font-size:12px;color:#6e7681">
          ${act.input_tokens.toLocaleString()} input + ${act.output_tokens.toLocaleString()} output = <strong style="color:#8b949e">${act.total_tokens.toLocaleString()}</strong> total tokens
        </div>`;
      }

      ahtml += `</div>`;
      document.getElementById('main-content').insertAdjacentHTML('afterbegin', ahtml);
    }).catch(() => {});

  // ── 5. Token Usage (legacy pipeline sessions) ──
  if (sess && sess.usage && Object.keys(sess.usage).length) {
    html += `<div class="card"><h2>Token Usage</h2>
      <table>
        <thead><tr><th>Step</th><th>Input</th><th>Output</th><th>Cache Read</th><th>Cache Write</th><th>Total</th></tr></thead>
        <tbody>`;
    let totIn = 0, totOut = 0, totCR = 0, totCW = 0;
    Object.entries(sess.usage).forEach(([key, u]) => {
      const label = key.replace(/_usage$/, '');
      const inp  = u.input_tokens || 0;
      const out  = u.output_tokens || 0;
      const cr   = u.cache_read_input_tokens || 0;
      const cw   = u.cache_creation_input_tokens || 0;
      const tot  = inp + out + cr + cw;
      totIn += inp; totOut += out; totCR += cr; totCW += cw;
      html += `<tr>
        <td>${escHtml(label)}</td>
        <td>${inp.toLocaleString()}</td>
        <td>${out.toLocaleString()}</td>
        <td>${cr.toLocaleString()}</td>
        <td>${cw.toLocaleString()}</td>
        <td>${tot.toLocaleString()}</td>
      </tr>`;
    });
    const totAll = totIn + totOut + totCR + totCW;
    html += `</tbody>
        <tfoot><tr class="totals-row">
          <td>TOTAL</td>
          <td>${totIn.toLocaleString()}</td>
          <td>${totOut.toLocaleString()}</td>
          <td>${totCR.toLocaleString()}</td>
          <td>${totCW.toLocaleString()}</td>
          <td>${totAll.toLocaleString()}</td>
        </tr></tfoot>
      </table></div>`;
  }

  // ── 5. Validation ──
  if (sess && (sess.validation_issues || sess.validation_warnings)) {
    html += `<div class="card"><h2>Validation</h2>`;
    if (sess.validation_issues) {
      html += `<div class="validation-issues"><div class="validation-label">Issues</div><pre>${escHtml(sess.validation_issues)}</pre></div>`;
    }
    if (sess.validation_warnings) {
      html += `<div class="validation-warnings"><div class="validation-label">Warnings</div><pre>${escHtml(sess.validation_warnings)}</pre></div>`;
    }
    html += `</div>`;
  }

  // ── 6. AI Review ──
  if (sess && sess.review) {
    html += `<div class="card"><h2>AI Review</h2><pre>${escHtml(sess.review)}</pre></div>`;
  }

  document.getElementById('main-content').innerHTML = html;
}

async function loadJob() {
  try {
    const r = await fetch('/api/job/' + encodeURIComponent(JOB_ID));
    if (!r.ok) {
      const d = await r.json().catch(() => ({}));
      document.getElementById('main-content').innerHTML =
        `<div id="error-msg">Job not found: ${escHtml(d.error || r.status)}</div>`;
      return;
    }
    const job = await r.json();
    renderPage(job);
  } catch(e) {
    document.getElementById('main-content').innerHTML =
      `<div id="error-msg">Failed to load job: ${escHtml(String(e))}</div>`;
  }
}

loadJob();

// If loaded inside the detail drawer, refresh parent job list after actions
function _notifyParent() {
  if (window.parent && window.parent !== window && window.parent.fetchJobs) {
    window.parent.fetchJobs();
    window.parent.closeDrawer();
  } else {
    location.href = '/';
  }
}
</script>
</body>
</html>
"""

# ── HTTP handler ───────────────────────────────────────────────────────────────

class Handler(http.server.BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        pass  # suppress per-request noise

    def _send_json(self, data: object, status: int = 200) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        return json.loads(raw) if raw else {}

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._serve_dashboard()
        elif self.path == "/api/jobs":
            self._api_jobs()
        elif self.path == "/api/models":
            self._send_json(fetch_models())
        elif self.path == "/api/worker-stream":
            self._api_worker_stream()
        elif self.path.startswith("/api/diff/"):
            self._api_diff()
        elif self.path.startswith("/api/activity/"):
            self._api_activity()
        elif self.path.startswith("/api/job/"):
            self._api_job_detail()
        elif self.path.startswith("/job/"):
            self._serve_job_detail()
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == "/api/submit":
            self._api_submit()
        elif self.path == "/api/cancel":
            self._api_cancel()
        elif self.path == "/api/accept":
            self._api_accept()
        elif self.path == "/api/accept-chain":
            self._api_accept_chain()
        elif self.path == "/api/stop-worker":
            self._api_stop_worker()
        elif self.path == "/api/reject":
            self._api_reject()
        elif self.path == "/api/abandon":
            self._api_abandon()
        elif self.path == "/api/set-status":
            self._api_set_status()
        elif self.path == "/api/set-chain":
            self._api_set_chain()
        else:
            self.send_error(404)

    # ── GET handlers ──

    def _serve_dashboard(self):
        html = HTML_TEMPLATE.replace("__DEFAULT_REPO__", json.dumps(DEFAULT_REPO)[1:-1])
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _api_jobs(self):
        try:
            self._send_json(read_jobs())
        except Exception as exc:
            self._send_json({"error": str(exc)}, 500)

    def _api_worker_stream(self):
        global _worker_running, _worker_proc
        with _worker_lock:
            if _worker_running:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(f'data: {json.dumps({"error": "Worker already running"})}\n\n'.encode())
                self.wfile.write(f'data: {json.dumps({"done": True, "rc": 1})}\n\n'.encode())
                return
            _worker_running = True

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        agentic_bin = AGENTIC_HOME / "bin" / "agentic"
        proc = None
        try:
            proc = subprocess.Popen(
                [str(agentic_bin), "worker-once"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                start_new_session=True,   # own process group — killpg kills the whole tree
            )
            with _worker_lock:
                _worker_proc = proc
            for line in iter(proc.stdout.readline, ""):
                msg = json.dumps({"line": line.rstrip("\n")})
                self.wfile.write(f"data: {msg}\n\n".encode())
                self.wfile.flush()
            proc.wait()
            done = json.dumps({"done": True, "rc": proc.returncode})
            self.wfile.write(f"data: {done}\n\n".encode())
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            try:
                self.wfile.write(f'data: {json.dumps({"error": str(exc)})}\n\n'.encode())
                self.wfile.write(f'data: {json.dumps({"done": True, "rc": 1})}\n\n'.encode())
            except Exception:
                pass
        finally:
            if proc and proc.poll() is None:
                try:
                    import os, signal as _sig
                    os.killpg(os.getpgid(proc.pid), _sig.SIGTERM)
                except Exception:
                    proc.terminate()
            with _worker_lock:
                _worker_running = False
                _worker_proc = None

    def _api_stop_worker(self):
        global _worker_running, _worker_proc
        with _worker_lock:
            if not _worker_running or _worker_proc is None:
                self._send_json({"ok": False, "error": "No worker running"})
                return
            proc = _worker_proc
        try:
            import os, signal as _sig
            os.killpg(os.getpgid(proc.pid), _sig.SIGTERM)
            self._send_json({"ok": True})
        except ProcessLookupError:
            self._send_json({"ok": True})   # already dead
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)})

    def _api_activity(self):
        job_id = self.path.split("/api/activity/", 1)[-1].strip("/")
        try:
            self._send_json(get_agent_activity(job_id))
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, 500)

    def _api_diff(self):
        job_id = self.path.split("/api/diff/", 1)[-1].strip("/")
        try:
            diff = get_diff(job_id)
            self._send_json({"ok": True, "diff": diff})
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, 404)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, 500)

    def _serve_job_detail(self):
        job_id = self.path.split("/job/", 1)[-1].strip("/")
        html = JOB_DETAIL_HTML.replace("__JOB_ID__", job_id)
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _api_job_detail(self):
        job_id = self.path.split("/api/job/", 1)[-1].strip("/")
        try:
            self._send_json(get_job_detail(job_id))
        except ValueError as exc:
            self._send_json({"error": str(exc)}, 404)
        except Exception as exc:
            self._send_json({"error": str(exc)}, 500)

    # ── POST handlers ──

    def _api_submit(self):
        try:
            body       = self._read_body()
            request    = str(body.get("request", "")).strip()
            repo       = str(body.get("repo", "")).strip() or DEFAULT_REPO
            priority   = int(body.get("priority", 0))
            model_hint = str(body.get("model_hint", "auto")).strip()
            after      = str(body.get("after", "")).strip()
            if not request: self._send_json({"ok": False, "error": "request is required"}, 400); return
            if not repo:    self._send_json({"ok": False, "error": "repo path could not be determined"}, 400); return
            job_id = submit_job(request, repo, priority, model_hint, after or None)
            self._send_json({"ok": True, "id": job_id})
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, 400)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, 500)

    def _api_cancel(self):
        try:
            job_id = str(self._read_body().get("id", "")).strip()
            if not job_id: self._send_json({"ok": False, "error": "id required"}, 400); return
            cancel_job(job_id)
            self._send_json({"ok": True})
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, 404)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, 500)

    def _api_accept(self):
        try:
            job_id = str(self._read_body().get("id", "")).strip()
            if not job_id: self._send_json({"ok": False, "error": "id required"}, 400); return
            msg = accept_job(job_id)
            self._send_json({"ok": True, "message": msg})
        except (ValueError, RuntimeError) as exc:
            self._send_json({"ok": False, "error": str(exc)}, 400)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, 500)

    def _api_accept_chain(self):
        try:
            job_id = str(self._read_body().get("id", "")).strip()
            if not job_id: self._send_json({"ok": False, "error": "id required"}, 400); return
            result = accept_chain(job_id)
            self._send_json({"ok": True, **result})
        except (ValueError, RuntimeError) as exc:
            self._send_json({"ok": False, "error": str(exc)}, 400)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, 500)

    def _api_reject(self):
        try:
            job_id = str(self._read_body().get("id", "")).strip()
            if not job_id: self._send_json({"ok": False, "error": "id required"}, 400); return
            reject_job(job_id)
            self._send_json({"ok": True})
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, 404)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, 500)

    def _api_abandon(self):
        try:
            job_id = str(self._read_body().get("id", "")).strip()
            if not job_id: self._send_json({"ok": False, "error": "id required"}, 400); return
            abandon_job(job_id)
            self._send_json({"ok": True})
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, 404)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, 500)

    def _api_set_status(self):
        try:
            body = self._read_body()
            job_id = str(body.get("id", "")).strip()
            new_status = str(body.get("status", "")).strip()
            if not job_id or not new_status:
                self._send_json({"ok": False, "error": "id and status required"}, 400); return
            set_job_status(job_id, new_status)
            self._send_json({"ok": True})
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, 400)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, 500)

    def _api_set_chain(self):
        try:
            body = self._read_body()
            job_id    = str(body.get("id", "")).strip()
            parent_id = body.get("parent_id")  # None = clear chain
            if parent_id is not None:
                parent_id = str(parent_id).strip() or None
            if not job_id:
                self._send_json({"ok": False, "error": "id required"}, 400); return
            set_chain(job_id, parent_id)
            self._send_json({"ok": True})
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, 400)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, 500)


# ── Entry point ────────────────────────────────────────────────────────────────

DEFAULT_REPO = os.getcwd()
PID_FILE     = AGENTIC_HOME / "serve.pid"

class _Server(http.server.ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        # Swallow disconnects — these are normal when browsers close SSE streams
        # or cancel requests; they are not actionable errors.
        if issubclass(sys.exc_info()[0], (ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 4080
    queue_init()

    PID_FILE.write_text(f"{os.getpid()}:{port}")
    atexit.register(lambda: PID_FILE.unlink(missing_ok=True))
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    server = _Server(("127.0.0.1", port), Handler)
    print(f"agentic dashboard → http://localhost:{port}")
    print("agentic serve stop  — to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
