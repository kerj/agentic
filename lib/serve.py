#!/usr/bin/env python3
"""
agentic queue dashboard — serve.py
Full local web UI: submit, run worker, view diff, accept/reject — no terminal needed.
"""

import atexit
import http.server
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from job_queue import (
    AGENTIC_HOME, STATES,
    queue_init, submit_job, find_job, read_jobs,
    cancel_job, accept_job, accept_chain, abandon_job,
    set_chain, set_job_status, reject_job, delete_job, review_job, submit_review_job,
    get_diff, get_agent_activity, fetch_models, get_ollama_models,
    get_job_chain, get_job_detail, get_job_full, get_repos,
)
import settings as _settings
import channels as _channels
import planner as _planner

# ── Worker state (global, guarded by _worker_lock) ────────────────────────────

_worker_lock    = threading.Lock()
_worker_running = False
_worker_proc: "subprocess.Popen[str] | None" = None

# Line buffer + condition so reconnecting SSE clients can catch up.
# Primary stream appends here; waiters wake on _worker_cond.notify_all().
_worker_log: list[str] = []
_worker_rc:  int | None = None
_worker_cond = threading.Condition(_worker_lock)

# ── Planning-channel SSE state (DICT-KEYED, per-thread) ────────────────────────
# Unlike the singleton worker buffer above, planning streams are keyed by thread
# id so two threads (or a thread + a running job) can stream concurrently. Each
# tid owns a list of event dicts, a "done" flag, and a condition for waiters.
# Reconnecting clients replay the buffer; closing the browser does not stop the
# run (the daemon thread owns the subprocess lifetime).
_ask_lock = threading.Lock()
_ask_log: dict[str, list[dict]] = {}
_ask_done: dict[str, bool] = {}
_ask_cond: dict[str, threading.Condition] = {}
_ask_running: dict[str, bool] = {}


def _ask_buf(tid: str) -> threading.Condition:
    """Get (creating if needed) the per-thread condition guarding its buffer."""
    with _ask_lock:
        cond = _ask_cond.get(tid)
        if cond is None:
            cond = threading.Condition()
            _ask_cond[tid] = cond
            _ask_log.setdefault(tid, [])
            _ask_done.setdefault(tid, False)
            _ask_running.setdefault(tid, False)
        return cond


def _ask_emit(tid: str, ev: dict) -> None:
    """Append one event to a thread's buffer and wake all SSE tailers."""
    cond = _ask_buf(tid)
    with cond:
        _ask_log[tid].append(ev)
        cond.notify_all()


def _ask_finish(tid: str) -> None:
    """Mark a thread's stream complete and wake tailers a final time."""
    cond = _ask_buf(tid)
    with cond:
        _ask_done[tid] = True
        _ask_running[tid] = False
        cond.notify_all()

# ── HTML template ──────────────────────────────────────────────────────────────

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>agentic queue</title>
<link rel="stylesheet" href="/static/app.css">
</head>
<body>

<header>
  <div class="dot pulse"></div>
  <h1>agentic</h1>
  __LOCAL_BADGE__
  <div id="view-toggle">
    <button id="vt-queue" class="vt-btn active" onclick="setView('queue')">Queue</button>
    <button id="vt-channels" class="vt-btn" onclick="setView('channels')">Channels</button>
  </div>
  <button id="run-btn" onclick="runWorker(false)">▶ Run Worker</button>
  <button id="run-all-btn" onclick="runWorker(true)" style="background:#1f4391;color:#88b4ff;padding:6px 14px;border-radius:6px;border:none;cursor:pointer;font-size:13px;font-weight:500;margin-left:4px;">▶▶ Run All</button>
  <button id="stop-btn" onclick="stopWorker()" style="display:none;background:#6d2120;color:#f47067;padding:6px 14px;border-radius:6px;border:1px solid #6d2120;cursor:pointer;font-size:13px;font-weight:500;margin-left:4px;">■ Stop</button>
  <button id="settings-btn" onclick="openSettings()" title="Settings" style="display:none;background:none;border:1px solid #21262d;color:#8b949e;padding:6px 10px;border-radius:6px;cursor:pointer;font-size:14px;margin-left:4px;">⚙</button>
  <span id="age">loading…</span>
</header>

<div id="settings-overlay" onclick="if(event.target===this)closeSettings()" style="display:none;position:fixed;inset:0;background:rgba(1,4,9,.7);z-index:200;align-items:flex-start;justify-content:center;overflow-y:auto;padding:40px 16px">
  <div style="background:#0d1117;border:1px solid #21262d;border-radius:10px;width:100%;max-width:560px;box-shadow:0 16px 48px rgba(0,0,0,.5)">
    <div style="display:flex;align-items:center;gap:8px;padding:14px 18px;border-bottom:1px solid #21262d">
      <span style="font-size:15px;font-weight:600;color:#e6edf3">⚙ Local model settings</span>
      <span id="settings-applies" style="margin-left:auto;font-size:11px;color:#6e7681">applies to the next job — no restart</span>
      <button onclick="closeSettings()" style="background:none;border:none;color:#8b949e;cursor:pointer;font-size:18px;line-height:1">×</button>
    </div>
    <div id="settings-body" style="padding:16px 18px;max-height:70vh;overflow-y:auto"></div>
    <div style="display:flex;align-items:center;gap:10px;padding:12px 18px;border-top:1px solid #21262d">
      <span id="settings-status" style="font-size:12px;color:#8b949e"></span>
      <button onclick="saveSettings()" style="margin-left:auto;background:#1f6feb;color:#fff;border:none;padding:7px 16px;border-radius:6px;cursor:pointer;font-size:13px;font-weight:500">Save</button>
    </div>
  </div>
</div>

<div id="dirpicker-overlay" onclick="if(event.target===this)closeDirPicker()" style="display:none;position:fixed;inset:0;background:rgba(1,4,9,.75);z-index:300;align-items:flex-start;justify-content:center;overflow-y:auto;padding:48px 16px">
  <div style="background:#0d1117;border:1px solid #21262d;border-radius:10px;width:100%;max-width:560px;box-shadow:0 16px 48px rgba(0,0,0,.6)">
    <div style="display:flex;align-items:center;gap:8px;padding:14px 18px;border-bottom:1px solid #21262d">
      <span style="font-size:15px;font-weight:600;color:#e6edf3">📁 Choose project directory</span>
      <button onclick="closeDirPicker()" style="margin-left:auto;background:none;border:none;color:#8b949e;cursor:pointer;font-size:18px;line-height:1">×</button>
    </div>
    <div style="padding:10px 18px;border-bottom:1px solid #21262d">
      <div id="dirpicker-path" style="font-size:12px;color:#88b4ff;font-family:ui-monospace,monospace;word-break:break-all">/</div>
    </div>
    <div id="dirpicker-list" style="padding:8px 10px;max-height:50vh;overflow-y:auto"></div>
    <div style="display:flex;align-items:center;gap:10px;padding:12px 18px;border-top:1px solid #21262d">
      <span id="dirpicker-hint" style="font-size:11px;color:#6e7681"></span>
      <button onclick="closeDirPicker()" style="margin-left:auto;background:#21262d;border:1px solid #30363d;color:#e6edf3;padding:7px 14px;border-radius:6px;cursor:pointer;font-size:13px">Cancel</button>
      <button id="dirpicker-use" onclick="useCurrentDir()" style="background:#238636;color:#fff;border:none;padding:7px 16px;border-radius:6px;cursor:pointer;font-size:13px;font-weight:500">Use this folder</button>
    </div>
  </div>
</div>

<div id="peek-pop" onclick="if(event.target===this)closePeek()" style="display:none;position:fixed;inset:0;background:rgba(1,4,9,.75);z-index:320;align-items:flex-start;justify-content:center;overflow-y:auto;padding:48px 16px">
  <div style="background:#0d1117;border:1px solid #21262d;border-radius:10px;width:100%;max-width:760px;box-shadow:0 16px 48px rgba(0,0,0,.6)">
    <div style="display:flex;align-items:center;gap:8px;padding:12px 16px;border-bottom:1px solid #21262d">
      <span style="font-size:13px">📄</span>
      <span id="peek-title" style="font-size:13px;font-weight:600;color:#88b4ff;font-family:ui-monospace,monospace;word-break:break-all"></span>
      <button id="peek-edit" title="Open in VS Code" style="margin-left:auto;display:none;background:#21262d;border:1px solid #30363d;color:#e6edf3;padding:5px 12px;border-radius:6px;cursor:pointer;font-size:12px">Open in editor</button>
      <button onclick="closePeek()" style="background:none;border:none;color:#8b949e;cursor:pointer;font-size:18px;line-height:1">×</button>
    </div>
    <div id="peek-body" style="max-height:60vh;overflow:auto;padding:10px 0"></div>
  </div>
</div>

<div class="container" id="queue-view">
  <div class="card">
    <h2>Submit Job</h2>
    <select id="repo-select" style="width:100%;margin-bottom:8px;font-family:monospace;font-size:12px" onchange="onRepoChange(this.value)">
      <option value="__DEFAULT_REPO_HTML__">__DEFAULT_REPO_HTML__</option>
    </select>
    <textarea id="req" placeholder="Describe the change you want…" rows="3"></textarea>
    <div class="form-row">
      <label class="field"><span class="field-label">Priority</span>
        <select id="priority">
          <option value="0">0 — normal</option>
          <option value="1">1 — high</option>
          <option value="5">5 — urgent</option>
        </select>
      </label>
      <button class="btn btn-green" style="align-self:flex-end;margin-left:auto" onclick="submitJob()">Submit</button>
    </div>
    <div class="form-row" style="margin-top:6px">
      <label class="field" style="flex:1"><span class="field-label">Chain after</span>
        <select id="after">
          <option value="">— no chain (independent job) —</option>
        </select>
      </label>
    </div>
    <div id="exec-mode-hint" style="font-size:11px;color:#6e7681;margin-top:6px"></div>
    <div style="font-size:11px;color:#6e7681;margin-top:2px">Cmd+Enter / Ctrl+Enter to submit · the worker runs this job using the <b>execution mode</b> set in ⚙ Settings</div>
  </div>

  <div class="filters">
    <button class="filter-btn active" data-state="all"       onclick="setFilter('all',this)">All       <span id="cnt-all"       class="badge">0</span></button>
    <button class="filter-btn"        data-state="pending"   onclick="setFilter('pending',this)">Pending   <span id="cnt-pending"   class="badge">0</span></button>
    <button class="filter-btn"        data-state="running"   onclick="setFilter('running',this)">Running   <span id="cnt-running"   class="badge">0</span></button>
    <button class="filter-btn"        data-state="done"      onclick="setFilter('done',this)">Done      <span id="cnt-done"      class="badge">0</span></button>
    <button class="filter-btn"        data-state="merged"    onclick="setFilter('merged',this)">Merged    <span id="cnt-merged"    class="badge">0</span></button>
    <button class="filter-btn"        data-state="failed"    onclick="setFilter('failed',this)">Failed    <span id="cnt-failed"    class="badge">0</span></button>
    <button class="filter-btn"        data-state="abandoned" onclick="setFilter('abandoned',this)">Abandoned <span id="cnt-abandoned" class="badge">0</span></button>
    <button class="filter-btn"        data-state="cancelled" onclick="setFilter('cancelled',this)">Cancelled <span id="cnt-cancelled" class="badge">0</span></button>
  </div>

  <input type="text" id="job-search" placeholder="Search jobs…" style="width:100%;margin-bottom:12px" oninput="searchQuery=this.value.trim().toLowerCase();renderJobs()">
  <div id="job-list"></div>
</div>

<!-- ── Channels view (three zones: left rail · center chat · header) ── -->
<div id="channels-view">
  <div id="ch-rail">
    <div id="ch-rail-head">
      <button class="btn btn-green" onclick="newChannel()">+ Channel</button>
      <button class="btn btn-ghost" onclick="loadChannels()">↻</button>
    </div>
    <div id="ch-tree"><div style="padding:14px;color:#6e7681;font-size:12px">Loading…</div></div>
    <div id="ch-citations">
      <div id="ch-citations-head">Citations</div>
      <div id="ch-citations-list"></div>
    </div>
  </div>
  <div id="ch-main">
    <div id="ch-header">
      <span class="ch-title" id="ch-active-title">No thread selected</span>
      <span id="ch-plan-label" class="ch-plan-label" style="display:none" title="This is THIS thread's planning backend — separate from the global job execution mode in ⚙ Settings. Plan on Cloud while jobs run Local (or vice-versa). Remembered per thread.">Planning ⓘ</span>
      <select id="ch-mode" onchange="saveThreadModel()" style="display:none">
        <option value="local">local</option>
        <option value="cloud">cloud</option>
      </select>
      <select id="ch-model" onchange="saveThreadModel()" style="display:none"></select>
      <span id="ch-index-stat"></span>
      <button class="btn btn-ghost" id="ch-reindex-btn" onclick="reindexChannel()" style="display:none" title="Re-scan the repo to refresh the symbol index that powers instant 'what exists' answers. Use this after you've changed code without committing — the index auto-refreshes on commit, but not on uncommitted edits.">Refresh Index</button>
      <button class="btn btn-blue" id="ch-derive-btn" onclick="deriveJobs()" style="display:none">Make jobs ▸</button>
    </div>
    <div id="ch-transcript">
      <div id="ch-empty">Create a channel for a repo, then a thread, and ask a question grounded in your code.</div>
    </div>
    <div id="ch-input-row" style="display:none">
      <textarea id="ch-input" placeholder="Ask about this codebase…" rows="2" onkeydown="if((event.metaKey||event.ctrlKey)&&event.key==='Enter')askQuestion()"></textarea>
      <label id="ch-dig" title="Force a full grounded read-agent answer (opens real files, cites code) instead of the cheap instant index lookup — even for a simple question. Costs more, but digs into the actual code. Use it when the quick answer felt thin."><input type="checkbox" id="ch-dig-cb"> Dig deeper</label>
      <button class="btn btn-green" onclick="askQuestion()">Send</button>
    </div>
  </div>
</div>

<!-- ── Proposal drawer (editable cards → submit-all-as-chain) ── -->
<div id="prop-tab" onclick="restoreProp()" title="Show derived jobs">📋 Derived jobs <span id="prop-tab-count"></span></div>
<div id="prop-drawer">
  <div id="prop-head">
    <span>Derived jobs</span>
    <span id="prop-summary" style="font-weight:400;font-size:12px;color:#8b949e"></span>
    <button class="btn btn-ghost" style="margin-left:auto" onclick="minimizeProp()" title="Minimize (keep, tuck to the side)">▸ minimize</button>
    <button class="btn btn-ghost" onclick="closeProp()" title="Close and discard this view">✕</button>
  </div>
  <div id="prop-body"></div>
  <div id="prop-foot">
    <span id="prop-status" style="font-size:12px;color:#8b949e"></span>
    <button class="btn btn-green" style="margin-left:auto" onclick="submitProposal()">Submit all as chain</button>
  </div>
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
  <div id="log-resize"></div>
  <div id="log-header">
    <div class="dot pulse" id="log-dot" style="background:#f0883e"></div>
    <span id="log-title">Worker output</span>
    <span id="log-tokens" style="margin-left:auto;font-weight:400;font-size:12px;color:#8b949e;font-variant-numeric:tabular-nums;display:none"></span>
    <button id="log-collapse" title="Collapse">⌄</button>
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
    <div id="review-panel">
      <div id="review-comments-list"></div>
      <div id="review-composer">
        <div id="review-composer-label"></div>
        <textarea id="review-textarea" placeholder="Leave a comment…" rows="3"></textarea>
        <div class="review-actions">
          <button class="btn btn-ghost btn-sm" onclick="cancelReviewComment()">Cancel</button>
          <button class="btn btn-primary btn-sm" onclick="saveReviewComment()">Save comment</button>
        </div>
      </div>
      <div id="review-submit-row">
        <span id="review-count"></span>
        <button class="btn btn-primary" onclick="submitReview()">Submit Review</button>
      </div>
    </div>
  </div>
</div>

<div id="toast"></div>

<script>window.AGENTIC_CFG={defaultRepo:__DEFAULT_REPO_JS__,isLocal:__IS_LOCAL__};</script>
<script src="/static/app.js"></script>
</body>
</html>
"""

JOB_DETAIL_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>agentic job detail</title>
<script>window.AGENTIC_CFG={jobId:"__JOB_ID__"};</script>
<link rel="stylesheet" href="/static/job.css">
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

<script src="/static/job.js"></script>
</body>
</html>
"""

# ── HTTP handler ───────────────────────────────────────────────────────────────

class Handler(http.server.BaseHTTPRequestHandler):

    def log_message(self, format: str, *args: object) -> None:
        pass  # suppress per-request noise

    def _send_json(self, data: object, status: int = 200) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        return json.loads(raw) if raw else {}

    def do_GET(self) -> None:
        if self.path in ("/", "/index.html"):
            self._serve_dashboard()
        elif self.path.startswith("/static/"):
            self._serve_static()
        elif self.path == "/api/jobs":
            self._api_jobs()
        elif self.path == "/api/repos":
            self._send_json(get_repos(default_repo()))
        elif self.path == "/api/ollama-models":
            self._send_json(get_ollama_models() if is_local() else [])
        elif self.path == "/api/models":
            self._send_json(fetch_models())
        elif self.path == "/api/settings":
            self._api_get_settings()
        elif self.path == "/api/browse" or self.path.startswith("/api/browse?"):
            self._api_browse()
        elif self.path.startswith("/api/peek?"):
            self._api_peek()
        elif self.path == "/api/worker-stream":
            self._api_worker_stream()
        elif self.path == "/api/worker-status":
            self._send_json({"running": _worker_running})
        elif self.path == "/api/channels":
            self._api_channels_list()
        elif self.path == "/api/channels/models":
            self._api_channels_models()
        elif self.path.startswith("/api/ask-stream"):
            self._api_ask_stream()
        elif self.path.startswith("/api/channel/"):
            self._api_channel_get()
        elif self.path.startswith("/api/diff/"):
            self._api_diff()
        elif self.path.startswith("/api/activity/"):
            self._api_activity()
        elif self.path.startswith("/api/chain/"):
            self._api_chain()
        elif self.path.startswith("/api/job-full/"):
            self._api_job_full()
        elif self.path.startswith("/api/job/"):
            self._api_job_detail()
        elif self.path.startswith("/job/"):
            self._serve_job_detail()
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        if self.path == "/api/submit":
            self._api_submit()
        elif self.path == "/api/cancel":
            self._api_cancel()
        elif self.path == "/api/accept":
            self._api_accept()
        elif self.path == "/api/accept-chain":
            self._api_accept_chain()
        elif self.path == "/api/review":
            self._api_review()
        elif self.path == "/api/apply-to-tree":
            self._api_apply_to_tree()
        elif self.path == "/api/stop-worker":
            self._api_stop_worker()
        elif self.path == "/api/reject":
            self._api_reject()
        elif self.path == "/api/delete":
            self._api_delete()
        elif self.path == "/api/abandon":
            self._api_abandon()
        elif self.path == "/api/set-status":
            self._api_set_status()
        elif self.path == "/api/set-chain":
            self._api_set_chain()
        elif self.path == "/api/settings":
            self._api_save_settings()
        elif self.path == "/api/secrets":
            self._api_save_secret()
        elif self.path == "/api/channel/create":
            self._api_channel_create()
        elif self.path.startswith("/api/channel/"):
            self._api_channel_post()
        else:
            self.send_error(404)

    # ── GET handlers ──

    def _serve_static(self) -> None:
        """Serve UI assets from lib/static/ under AGENTIC_APP (the baked source dir
        in Docker; ~/.agentic natively). Confined to that dir — no traversal."""
        from urllib.parse import urlparse, unquote
        rel = unquote(urlparse(self.path).path[len("/static/"):])
        app = Path(os.environ.get("AGENTIC_APP", str(AGENTIC_HOME)))
        root = (app / "lib" / "static").resolve()
        target = (root / rel).resolve()
        if not (target == root or root in target.parents) or not target.is_file():
            self.send_error(404); return
        ctypes = {".css": "text/css; charset=utf-8",
                  ".js": "application/javascript; charset=utf-8",
                  ".map": "application/json"}
        ctype = ctypes.get(target.suffix, "application/octet-stream")
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # Short cache; the URL carries a build stamp for hard refresh on deploy.
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _serve_dashboard(self) -> None:
        _local = is_local()
        _repo  = default_repo()
        # Active-model badge in the header — shows the model jobs CURRENTLY run on,
        # per the execution mode + the matching Settings knob. Local = amber 🏠,
        # cloud = blue ☁. (Mirrors so you always see what's running your jobs.)
        if _local:
            mode_badge = (
                f'<span style="font-size:11px;background:#2a1f00;color:#d29922;'
                f'border:1px solid #5a3e1b;border-radius:10px;padding:2px 8px;margin-left:4px">'
                f'🏠 local · {local_model()}</span>'
            )
        else:
            _cm = _settings.load().get("cloud_model", "auto")
            mode_badge = (
                f'<span style="font-size:11px;background:#0d2440;color:#58a6ff;'
                f'border:1px solid #1f4391;border-radius:10px;padding:2px 8px;margin-left:4px">'
                f'☁ cloud · {_cm}</span>'
            )
        html = (HTML_TEMPLATE
                .replace("__DEFAULT_REPO_HTML__", _repo)
                .replace("__DEFAULT_REPO_JS__",   json.dumps(_repo))
                .replace("__LOCAL_BADGE__",        mode_badge)
                .replace("__IS_LOCAL__",           "true" if _local else "false")
                .replace("__MODEL_FIELD__",        ""))
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _api_jobs(self) -> None:
        try:
            self._send_json(read_jobs())
        except Exception as exc:
            self._send_json({"error": str(exc)}, 500)

    def _api_worker_stream(self) -> None:
        global _worker_running, _worker_proc, _worker_log, _worker_rc

        with _worker_lock:
            already_running = _worker_running
            if not already_running:
                _worker_running = True
                _worker_log = []
                _worker_rc  = None

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        def _send(obj: dict) -> bool:
            try:
                self.wfile.write(f"data: {json.dumps(obj)}\n\n".encode())
                self.wfile.flush()
                return True
            except (BrokenPipeError, ConnectionResetError):
                return False

        if already_running:
            # Reconnect: replay buffered lines then tail until worker finishes.
            cursor = 0
            while True:
                with _worker_cond:
                    # Drain any new lines since last check
                    snapshot = _worker_log[cursor:]
                    done_rc  = _worker_rc
                for line in snapshot:
                    if not _send({"line": line, "replayed": cursor == 0}):
                        return
                cursor += len(snapshot)
                if done_rc is not None:
                    _send({"done": True, "rc": done_rc})
                    return
                if not snapshot:
                    # Wait for the primary thread to append more lines
                    with _worker_cond:
                        _worker_cond.wait(timeout=2.0)
            return

        # Primary connection: spin up the worker in a daemon thread so it
        # outlives this SSE connection. The thread owns the process lifetime;
        # this handler just tails the shared buffer like any other reconnect.
        def _run_worker():
            global _worker_running, _worker_proc, _worker_rc
            # bin/agentic is APP SOURCE — resolve via AGENTIC_APP (defaults to
            # AGENTIC_HOME for native; Docker sets it to the baked /opt/agentic).
            agentic_bin = Path(os.environ.get("AGENTIC_APP", str(AGENTIC_HOME))) / "bin" / "agentic"
            # Re-resolve mode/model from settings.json AT SPAWN TIME (not server
            # start) so flipping mode in the UI takes effect on the very next job
            # with no restart. worker.sh reads AGENTIC_LOCAL to choose ollama vs
            # the claude CLI; AGENTIC_LOCAL_MODEL selects the local model.
            _cfg = _settings.load()
            _env = {
                **os.environ,
                "AGENTIC_LOCAL":       "1" if _cfg.get("mode") == "local" else "",
                "AGENTIC_LOCAL_MODEL": _cfg.get("local_model", "qwen-coder:latest"),
            }
            # CLOUD execution: the claude CLI authenticates via ANTHROPIC_API_KEY
            # (in secrets.json, not the env) — inject it. The cloud MODEL is now a
            # global Settings knob (cloud_model), resolved at RUN time → passed as
            # AGENTIC_MODEL so worker.sh's --model flag uses it.
            if _cfg.get("mode") != "local":
                _key = _settings.get_secret("ANTHROPIC_API_KEY")
                if _key:
                    _env["ANTHROPIC_API_KEY"] = _key
                _env["AGENTIC_MODEL"] = _cfg.get("cloud_model", "auto")
            proc = None
            try:
                proc = subprocess.Popen(
                    [str(agentic_bin), "worker-once"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    start_new_session=True,
                    env=_env,
                )
                with _worker_cond:
                    _worker_proc = proc
                for line in iter(proc.stdout.readline, "") if proc.stdout else []:
                    with _worker_cond:
                        _worker_log.append(line.rstrip("\n"))
                        _worker_cond.notify_all()
                proc.wait()
            except Exception:
                pass
            finally:
                rc = proc.returncode if proc else 1
                with _worker_cond:
                    _worker_rc      = rc
                    _worker_running = False
                    _worker_proc    = None
                    _worker_cond.notify_all()

        t = threading.Thread(target=_run_worker, daemon=True)
        t.start()

        # Now tail the buffer exactly like a reconnecting client.
        cursor = 0
        while True:
            with _worker_cond:
                snapshot = _worker_log[cursor:]
                done_rc  = _worker_rc
            for line in snapshot:
                # Live progress sentinel (from stream_parser) → structured event
                # for the header counter, NOT a log line.
                if line.startswith("\x01PROGRESS "):
                    try:
                        prog = json.loads(line[len("\x01PROGRESS "):])
                    except Exception:
                        prog = None
                    if prog is not None and not _send({"progress": prog}):
                        return
                    continue
                if not _send({"line": line}):
                    return  # browser disconnected — worker keeps running
            cursor += len(snapshot)
            if done_rc is not None:
                _send({"done": True, "rc": done_rc})
                return
            if not snapshot:
                with _worker_cond:
                    _worker_cond.wait(timeout=2.0)

    def _api_stop_worker(self) -> None:
        global _worker_running, _worker_proc
        with _worker_lock:
            if not _worker_running or _worker_proc is None:
                self._send_json({"ok": False, "error": "No worker running"})
                return
            proc = _worker_proc
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            self._send_json({"ok": True})
        except ProcessLookupError:
            self._send_json({"ok": True})   # already dead
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)})

    def _api_activity(self) -> None:
        job_id = self.path.split("/api/activity/", 1)[-1].strip("/")
        try:
            self._send_json(get_agent_activity(job_id))
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, 500)

    def _api_chain(self) -> None:
        job_id = self.path.split("/api/chain/", 1)[-1].strip("/")
        try:
            self._send_json(get_job_chain(job_id))
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, 404)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, 500)

    def _api_diff(self) -> None:
        job_id = self.path.split("/api/diff/", 1)[-1].strip("/")
        try:
            diff = get_diff(job_id)
            self._send_json({"ok": True, "diff": diff})
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, 404)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, 500)

    def _serve_job_detail(self) -> None:
        job_id = self.path.split("/job/", 1)[-1].strip("/")
        html = JOB_DETAIL_HTML.replace("__JOB_ID__", job_id)
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _api_job_full(self) -> None:
        job_id = self.path.split("/api/job-full/", 1)[-1].strip("/")
        try:
            self._send_json(get_job_full(job_id))
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, 404)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, 500)

    def _api_job_detail(self) -> None:
        job_id = self.path.split("/api/job/", 1)[-1].strip("/")
        try:
            self._send_json(get_job_detail(job_id))
        except ValueError as exc:
            self._send_json({"error": str(exc)}, 404)
        except Exception as exc:
            self._send_json({"error": str(exc)}, 500)

    # ── POST handlers ──

    def _api_submit(self) -> None:
        try:
            body       = self._read_body()
            request    = str(body.get("request", "")).strip()
            repo       = str(body.get("repo", "")).strip() or default_repo()
            priority   = int(body.get("priority", 0))
            model_hint = str(body.get("model_hint", "auto")).strip()
            after      = str(body.get("after", "")).strip()
            if not request:
                self._send_json({"ok": False, "error": "request is required"}, 400)
                return
            if not repo:
                self._send_json({"ok": False, "error": "repo path could not be determined"}, 400)
                return
            job_id, job_name = submit_job(request, repo, priority, model_hint, after or None)
            self._send_json({"ok": True, "id": job_id, "name": job_name})
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, 400)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, 500)

    def _api_cancel(self) -> None:
        try:
            job_id = str(self._read_body().get("id", "")).strip()
            if not job_id:
                self._send_json({"ok": False, "error": "id required"}, 400)
                return
            cancel_job(job_id)
            self._send_json({"ok": True})
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, 404)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, 500)

    def _api_accept(self) -> None:
        try:
            body   = self._read_body()
            job_id = str(body.get("id", "")).strip()
            if not job_id:
                self._send_json({"ok": False, "error": "id required"}, 400)
                return
            msg = accept_job(job_id, acknowledge_risk=bool(body.get("acknowledge")))
            self._send_json({"ok": True, "message": msg})
        except (ValueError, RuntimeError) as exc:
            self._send_json({"ok": False, "error": str(exc)}, 400)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, 500)

    def _api_get_settings(self) -> None:
        """Schema + current values for the settings panel. Secrets are returned
        as status booleans only — the key itself never crosses to the browser."""
        try:
            self._send_json({
                "ok": True,
                "schema": _settings.schema_for_ui(),
                "num_ctx": _settings.model_num_ctx(),
                # Both model lists are always provided so you can set local OR
                # cloud model regardless of the current execution mode.
                "ollama_models": get_ollama_models(),
                "cloud_models": fetch_models(),
                "secrets": _settings.secrets_status(),
                "local_mode": is_local(),
                "browse_root": browse_root(),
            })
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, 500)

    def _api_browse(self) -> None:
        """Read-only directory browser for the project picker. Lists the immediate
        subdirectories of ?path= and marks which are git repos. CONFINED to
        browse_root(): a resolved path outside the root (e.g. via .. or a symlink)
        is rejected and snapped back to the root. Never reads file contents."""
        from urllib.parse import urlparse, parse_qs, unquote
        try:
            qs = parse_qs(urlparse(self.path).query)
            raw = unquote((qs.get("path") or [""])[0]).strip()
            root = Path(browse_root())

            # Resolve the requested path; default to the root. strict=False so a
            # not-yet-existing path doesn't throw — we validate existence below.
            target = Path(raw).resolve() if raw else root
            # Confinement: target must be the root or inside it. resolve() has
            # already collapsed any '..' and followed symlinks, so this catches
            # both traversal and symlink escape.
            try:
                inside = target == root or root in target.parents
            except Exception:
                inside = False
            if not inside or not target.is_dir():
                target = root

            entries = []
            try:
                for child in sorted(target.iterdir(), key=lambda p: p.name.lower()):
                    # Skip hidden dirs (keep .git out of the picker), non-dirs,
                    # and anything unreadable.
                    if not child.is_dir() or child.name.startswith("."):
                        continue
                    try:
                        is_repo = (child / ".git").exists()
                    except Exception:
                        is_repo = False
                    entries.append({"name": child.name,
                                    "path": str(child),
                                    "is_repo": is_repo})
            except PermissionError:
                pass

            parent = str(target.parent) if (target != root and root in target.parents) else None
            self._send_json({
                "ok": True,
                "root": str(root),
                "path": str(target),
                "parent": parent,
                "is_repo": (target / ".git").exists(),
                "entries": entries,
            })
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, 500)

    def _api_peek(self) -> None:
        """Read-only code peek for a citation, from a file in the channel's repo.
        CONFINED to that repo (resolve() + containment, like _api_browse) so a
        citation can't read outside the repo it belongs to.

        Query: ?cid=<channel>&file=<rel-or-abs>[&start=N&end=M][&symbol=name]
        - With start/end: show that range (+ context), cited lines highlighted.
        - WITHOUT a start: show the FULL file (e.g. a whole-file Read/ReadSymbol).
          If `symbol` is given, lines mentioning that symbol are highlighted as
          'relevant' so you can find what the citation was about.
        Returns the lines + absolute path (for an 'open in editor' deep-link)."""
        from urllib.parse import urlparse, parse_qs, unquote
        import re as _re
        try:
            qs = parse_qs(urlparse(self.path).query)
            cid    = (qs.get("cid")  or [""])[0].strip()
            rel    = unquote((qs.get("file") or [""])[0]).strip()
            symbol = unquote((qs.get("symbol") or [""])[0]).strip()
            raw_start = (qs.get("start") or [""])[0].strip()
            has_range = bool(raw_start) and raw_start != "0"
            if not cid or not rel:
                self._send_json({"ok": False, "error": "cid and file required"}, 400); return

            ch = _channels.channel_load(cid)
            if not ch:
                self._send_json({"ok": False, "error": "channel not found"}, 404); return
            repo = Path(ch["repo"]).resolve()

            # Resolve the cited file UNDER the repo (handles rel or abs); reject escapes.
            cand = Path(rel)
            target = (cand if cand.is_absolute() else (repo / cand)).resolve()
            if not (target == repo or repo in target.parents):
                self._send_json({"ok": False, "error": "path outside repo"}, 400); return
            # Graceful fallback: a stray citation may point at a DIRECTORY (an old
            # search-dir citation). If we know the symbol, find the file it's
            # declared in and peek that instead of erroring.
            if target.is_dir() and symbol:
                try:
                    import subprocess as _sp
                    r = _sp.run(["grep", "-rnwI",
                                 "--exclude-dir=.git", "--exclude-dir=node_modules",
                                 symbol, str(target)],
                                capture_output=True, text=True, timeout=15)
                    for ln in r.stdout.splitlines():
                        mm = _re.match(r"^([^:]+):(\d+):", ln)
                        if mm and Path(mm.group(1)).is_file():
                            target = Path(mm.group(1)).resolve(); break
                except Exception:
                    pass
            if not target.is_file():
                self._send_json({"ok": False, "error": "not a file"}, 400); return

            lines = target.read_text(errors="replace").splitlines()
            n = len(lines)
            MAX_LINES = 2000  # guard against peeking a giant file whole

            if has_range:
                # Ranged citation: window around it with a little context.
                start = max(1, min(int(raw_start), n))
                end   = max(start, min(int((qs.get("end") or [raw_start])[0] or start), n))
                lo, hi = max(1, start - 2), min(n, end + 2)
                snippet = [{"n": i, "text": lines[i - 1], "cited": start <= i <= end}
                           for i in range(lo, hi + 1)]
                self._send_json({"ok": True, "file": rel, "abspath": str(target),
                                 "start": start, "end": end, "whole": False,
                                 "lines": snippet})
                return

            # No specific line → show the FULL file (capped). If a symbol is known,
            # highlight the lines that mention it as the 'relevant' part.
            truncated = n > MAX_LINES
            shown = lines[:MAX_LINES]
            sym_re = _re.compile(r"\b" + _re.escape(symbol) + r"\b") if symbol else None
            first_hit = None
            snippet = []
            for idx, text in enumerate(shown, start=1):
                rel_hit = bool(sym_re and sym_re.search(text))
                if rel_hit and first_hit is None:
                    first_hit = idx
                snippet.append({"n": idx, "text": text, "cited": rel_hit})
            self._send_json({
                "ok": True, "file": rel, "abspath": str(target),
                "whole": True, "symbol": symbol or None,
                "first_relevant": first_hit, "truncated": truncated,
                "total_lines": n, "lines": snippet,
            })
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, 500)

    def _api_save_settings(self) -> None:
        """Validate + persist knob updates (settings.save ignores unknown/secret
        keys and clamps values). Applies to the next job — no restart."""
        try:
            body = self._read_body()
            updates = body.get("settings", body)  # accept {settings:{...}} or bare {...}
            if not isinstance(updates, dict):
                self._send_json({"ok": False, "error": "settings must be an object"}, 400)
                return
            resolved = _settings.save(updates)
            self._send_json({"ok": True, "settings": resolved})
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, 500)

    def _api_save_secret(self) -> None:
        """Set a secret write-only (e.g. the API key). Never echoed back."""
        try:
            body = self._read_body()
            name  = str(body.get("name", "")).strip()
            value = str(body.get("value", ""))
            allowed = {"ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"}
            if name not in allowed:
                self._send_json({"ok": False, "error": "unknown secret"}, 400)
                return
            _settings.set_secret(name, value)
            # The API key gates the live model list — drop the cache so the next
            # /api/models re-queries with the new key (instead of a stale fallback
            # cached before the key existed).
            try:
                from job_queue import invalidate_models_cache
                invalidate_models_cache()
            except Exception:
                pass
            self._send_json({"ok": True, "secrets": _settings.secrets_status()})
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, 500)

    def _api_review(self) -> None:
        try:
            body     = self._read_body()
            job_id   = str(body.get("job_id", "")).strip()
            comments = body.get("comments", [])
            if not job_id:
                self._send_json({"ok": False, "error": "job_id required"}, 400)
                return
            if not isinstance(comments, list) or not comments:
                self._send_json({"ok": False, "error": "comments must be a non-empty list"}, 400)
                return
            review_id, review_name = submit_review_job(job_id, comments)
            self._send_json({"ok": True, "id": review_id, "name": review_name})
        except (ValueError, RuntimeError) as exc:
            self._send_json({"ok": False, "error": str(exc)}, 400)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, 500)

    def _api_apply_to_tree(self) -> None:
        try:
            job_id = str(self._read_body().get("id", "")).strip()
            if not job_id:
                self._send_json({"ok": False, "error": "id required"}, 400)
                return
            msg = review_job(job_id)
            self._send_json({"ok": True, "message": msg})
        except (ValueError, RuntimeError) as exc:
            self._send_json({"ok": False, "error": str(exc)}, 400)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, 500)

    def _api_accept_chain(self) -> None:
        try:
            job_id = str(self._read_body().get("id", "")).strip()
            if not job_id:
                self._send_json({"ok": False, "error": "id required"}, 400)
                return
            result = accept_chain(job_id)
            self._send_json({"ok": True, **result})
        except (ValueError, RuntimeError) as exc:
            self._send_json({"ok": False, "error": str(exc)}, 400)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, 500)

    def _api_reject(self) -> None:
        try:
            job_id = str(self._read_body().get("id", "")).strip()
            if not job_id:
                self._send_json({"ok": False, "error": "id required"}, 400)
                return
            reject_job(job_id)
            self._send_json({"ok": True})
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, 404)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, 500)

    def _api_delete(self) -> None:
        try:
            job_id = str(self._read_body().get("id", "")).strip()
            if not job_id:
                self._send_json({"ok": False, "error": "id required"}, 400)
                return
            delete_job(job_id)
            self._send_json({"ok": True})
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, 404)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, 500)

    def _api_abandon(self) -> None:
        try:
            job_id = str(self._read_body().get("id", "")).strip()
            if not job_id:
                self._send_json({"ok": False, "error": "id required"}, 400)
                return
            abandon_job(job_id)
            self._send_json({"ok": True})
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, 404)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, 500)

    def _api_set_status(self) -> None:
        try:
            body = self._read_body()
            job_id = str(body.get("id", "")).strip()
            new_status = str(body.get("status", "")).strip()
            if not job_id or not new_status:
                self._send_json({"ok": False, "error": "id and status required"}, 400)
                return
            set_job_status(job_id, new_status)
            self._send_json({"ok": True})
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, 400)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, 500)

    def _api_set_chain(self) -> None:
        try:
            body = self._read_body()
            job_id    = str(body.get("id", "")).strip()
            parent_id = body.get("parent_id")  # None = clear chain
            if parent_id is not None:
                parent_id = str(parent_id).strip() or None
            if not job_id:
                self._send_json({"ok": False, "error": "id required"}, 400)
                return
            set_chain(job_id, parent_id)
            self._send_json({"ok": True})
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, 400)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, 500)

    # ── Planning-channel handlers ──
    #
    # All channel state lives under AGENTIC_HOME/channels/ via lib/channels.py
    # (storage) and is grounded/derived via lib/planner.py (engine). serve.py
    # only wires routes → module calls and streams SSE; the job-isolation rule
    # (no thread/transcript ever written into a job) is enforced in submit, where
    # only proposal job["request"] is passed to submit_job.

    def _channel_path_parts(self) -> list[str]:
        """Path segments after '/api/channel/' (query stripped)."""
        path = self.path.split("?", 1)[0]
        rest = path.split("/api/channel/", 1)[-1].strip("/")
        return [p for p in rest.split("/") if p]

    def _api_channels_list(self) -> None:
        """GET /api/channels — left-rail tree: channels (per repo) + their threads,
        each annotated with a quick symbol-index stat for the header."""
        try:
            out = []
            for ch in _channels.channels_list():
                cid = ch.get("id", "")
                threads = _channels.threads_list(cid) if cid else []
                out.append({**ch, "threads": threads})
            self._send_json({"ok": True, "channels": out})
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, 500)

    def _api_channels_models(self) -> None:
        """GET /api/channels/models — both cloud + local model lists, ALWAYS
        (this deliberately drops the is_local() gate: a thread may plan on cloud
        while jobs run local, or vice-versa)."""
        try:
            self._send_json({"ok": True, **_planner.model_lists()})
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, 500)

    def _api_channel_get(self) -> None:
        """GET /api/channel/<cid>/<tid> — thread header + transcript + citations.
        Also GET /api/channel/<cid> — channel header + threads + index stats."""
        parts = self._channel_path_parts()
        try:
            if len(parts) == 1:
                cid = parts[0]
                ch = _channels.channel_load(cid)
                threads = _channels.threads_list(cid)
                self._send_json({"ok": True, "header": ch, "threads": threads})
                return
            if len(parts) == 2:
                cid, tid = parts
                self._send_json({
                    "ok": True,
                    "header": _channels.thread_load(cid, tid),
                    "transcript": _channels.transcript_read(cid, tid),
                    "citations": _channels.citations_read(cid, tid),
                    "proposals": _channels.proposals_list(cid, tid),
                })
                return
            self.send_error(404)
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, 404)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, 500)

    def _api_channel_create(self) -> None:
        """POST /api/channel/create {repo} — idempotent per repo. Builds (or
        reuses) the symbol index and stamps index_head_sha."""
        try:
            body = self._read_body()
            repo = str(body.get("repo", "")).strip() or default_repo()
            if not repo:
                self._send_json({"ok": False, "error": "repo required"}, 400)
                return
            ch = _channels.channel_create(repo)
            cid = ch["id"]
            # Build/refresh the index and stamp it (cheap when HEAD hasn't moved).
            try:
                smap, head = _planner.cached_symbol_map(cid, ch["repo"], ch.get("profile"))
                if head and head != ch.get("index_head_sha"):
                    ch = _channels.channel_update(cid, index_head_sha=head)
                ch = {**ch, "index": _planner.symbol_map_stats(smap)}
            except Exception:
                pass
            self._send_json({"ok": True, **ch})
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, 500)

    def _api_channel_post(self) -> None:
        """Router for POST /api/channel/<cid>/... subroutes."""
        parts = self._channel_path_parts()
        try:
            # POST /api/channel/<cid>/thread/create
            if len(parts) == 3 and parts[1] == "thread" and parts[2] == "create":
                self._channel_thread_create(parts[0]); return
            # POST /api/channel/<cid>/reindex
            if len(parts) == 2 and parts[1] == "reindex":
                self._channel_reindex(parts[0]); return
            # POST /api/channel/<cid>/<tid>/set-model
            if len(parts) == 3 and parts[2] == "set-model":
                self._channel_set_model(parts[0], parts[1]); return
            # POST /api/channel/<cid>/<tid>/delete
            if len(parts) == 3 and parts[2] == "delete":
                self._channel_thread_delete(parts[0], parts[1]); return
            # POST /api/channel/<cid>/<tid>/derive  (SSE)
            if len(parts) == 3 and parts[2] == "derive":
                self._channel_derive(parts[0], parts[1]); return
            # POST /api/channel/<cid>/<tid>/submit
            if len(parts) == 3 and parts[2] == "submit":
                self._channel_submit(parts[0], parts[1]); return
            # POST /api/channel/<cid>/<tid>/proposal/<pid>  (save edited)
            if len(parts) == 4 and parts[2] == "proposal":
                self._channel_proposal_save(parts[0], parts[1], parts[3]); return
            self.send_error(404)
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, 404)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, 500)

    def _channel_thread_create(self, cid: str) -> None:
        body = self._read_body()
        mode = str(body.get("planning_mode", "")).strip() or _settings.get("planning_default_mode")
        model = str(body.get("planning_model", "")).strip()
        if not model:
            model = _settings.get("planning_default_model") or ""
        title = str(body.get("title", "")).strip()
        th = _channels.thread_create(cid, planning_mode=mode, planning_model=model, title=title)
        self._send_json({"ok": True, **th})

    def _channel_set_model(self, cid: str, tid: str) -> None:
        body = self._read_body()
        fields: dict[str, Any] = {}
        if "planning_mode" in body:
            fields["planning_mode"] = str(body.get("planning_mode", "")).strip() or "local"
        if "planning_model" in body:
            fields["planning_model"] = str(body.get("planning_model", "")).strip()
        if "title" in body:
            fields["title"] = str(body.get("title", "")).strip()
        th = _channels.thread_update(cid, tid, **fields)
        self._send_json({"ok": True, **th})

    def _channel_thread_delete(self, cid: str, tid: str) -> None:
        _channels.thread_delete(cid, tid)
        self._send_json({"ok": True})

    def _channel_reindex(self, cid: str) -> None:
        ch = _channels.channel_load(cid)
        smap, head = _planner.cached_symbol_map(cid, ch["repo"], ch.get("profile"), force=True)
        _channels.channel_update(cid, index_head_sha=head)
        self._send_json({"ok": True, "index_head_sha": head,
                         "index": _planner.symbol_map_stats(smap)})

    def _channel_proposal_save(self, cid: str, tid: str, pid: str) -> None:
        body = self._read_body()
        prop = body.get("proposal", body)
        if not isinstance(prop, dict):
            self._send_json({"ok": False, "error": "proposal must be an object"}, 400)
            return
        saved = _channels.proposal_save(cid, pid, prop)
        self._send_json({"ok": True, "proposal": saved})

    # ── Planning SSE: ask + derive ──

    def _sse_open(self) -> Any:
        """Start an event-stream response; return a _send(obj)->bool writer."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        def _send(obj: dict) -> bool:
            try:
                self.wfile.write(f"data: {json.dumps(obj)}\n\n".encode())
                self.wfile.flush()
                return True
            except (BrokenPipeError, ConnectionResetError):
                return False
        return _send

    def _stream_thread_buffer(self, tid: str, send: Any) -> None:
        """Tail a per-thread SSE buffer until the run completes or the client
        disconnects. Replays buffered events on (re)connect; survives browser
        close because the daemon thread owns the run."""
        cond = _ask_buf(tid)
        cursor = 0
        while True:
            with cond:
                snapshot = _ask_log[tid][cursor:]
                done = _ask_done[tid]
            for ev in snapshot:
                if not send(ev):
                    return  # browser gone — daemon thread keeps running
            cursor += len(snapshot)
            if done:
                send({"type": "done"})
                return
            if not snapshot:
                with cond:
                    cond.wait(timeout=2.0)

    def _api_ask_stream(self) -> None:
        """GET /api/ask-stream?cid&tid&q&dig — SSE grounding flow.
        Index path returns instantly (zero turns); agent path spawns a read-only
        subprocess in a daemon thread and streams its tool calls live."""
        from urllib.parse import urlparse, parse_qs, unquote
        qs = parse_qs(urlparse(self.path).query)
        cid = (qs.get("cid") or [""])[0]
        tid = (qs.get("tid") or [""])[0]
        q   = unquote((qs.get("q") or [""])[0])
        dig = (qs.get("dig") or ["0"])[0] == "1"
        if not (cid and tid and q.strip()):
            self._send_json({"ok": False, "error": "cid, tid, q required"}, 400)
            return
        try:
            channel = _channels.channel_load(cid)
            thread  = _channels.thread_load(cid, tid)
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, 404)
            return

        cond = _ask_buf(tid)
        with cond:
            already = _ask_running.get(tid, False)
            if not already:
                _ask_log[tid] = []
                _ask_done[tid] = False
                _ask_running[tid] = True

        send = self._sse_open()
        if already:
            # A run is already in flight for this thread — just tail it.
            self._stream_thread_buffer(tid, send)
            return

        # Record the user's question immediately (before the answer lands) so the
        # transcript reflects intent even if the run is interrupted.
        try:
            _channels.transcript_append(cid, tid, {"role": "user", "text": q})
        except Exception:
            pass

        channel_dict = {
            "cid": cid, "repo": channel["repo"],
            "profile": channel.get("profile"),
            "default_mode": _settings.get("planning_default_mode"),
        }
        thread_dict = dict(thread)
        thread_dict.setdefault("planning_max_turns", _settings.get("planning_max_turns"))

        def _run():
            try:
                smap, _ = _planner.cached_symbol_map(cid, channel["repo"], channel.get("profile"))
            except Exception:
                smap = None
            try:
                res = _planner.ask(
                    channel_dict, thread_dict, q,
                    symbol_map=smap, dig_deeper=dig,
                    on_event=lambda ev: _ask_emit(tid, ev),
                )
                _ask_emit(tid, {"type": "answer_final", **res})
                # Persist the grounded turn + harvested citations.
                try:
                    _channels.transcript_append(cid, tid, {
                        "role": "assistant",
                        "text": res.get("answer", ""),
                        "grounding": res.get("grounding"),
                        "turns": res.get("turns", 0),
                        "citations": res.get("citations", []),
                        "tokens": res.get("tokens", {}),
                        "badge": res.get("badge"),
                    })
                    if res.get("citations"):
                        _channels.citation_append(cid, tid, res["citations"])
                except Exception:
                    pass
            except Exception as exc:
                _ask_emit(tid, {"type": "error", "error": str(exc)})
            finally:
                _ask_finish(tid)

        threading.Thread(target=_run, daemon=True).start()
        self._stream_thread_buffer(tid, send)

    def _channel_derive(self, cid: str, tid: str) -> None:
        """POST /api/channel/<cid>/<tid>/derive — SSE. Runs the derivation agent +
        two-stage anchor verification, persists a proposal, streams its cards."""
        try:
            channel = _channels.channel_load(cid)
            thread  = _channels.thread_load(cid, tid)
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, 404)
            return

        cond = _ask_buf(tid)
        with cond:
            if _ask_running.get(tid, False):
                self._send_json({"ok": False, "error": "thread is busy"}, 409)
                return
            _ask_log[tid] = []
            _ask_done[tid] = False
            _ask_running[tid] = True

        transcript_text = self._transcript_text(cid, tid)
        channel_dict = {
            "cid": cid, "repo": channel["repo"],
            "profile": channel.get("profile"),
            "default_mode": _settings.get("planning_default_mode"),
        }
        thread_dict = dict(thread)
        thread_dict.setdefault("planning_max_turns", _settings.get("planning_max_turns"))

        send = self._sse_open()

        def _run():
            try:
                prop = _planner.derive(
                    channel_dict, thread_dict, transcript_text,
                    on_event=lambda ev: _ask_emit(tid, ev),
                )
                jobs = prop.get("jobs", [])
                created = _channels.proposal_create(
                    cid, tid,
                    jobs=[{"title": j.get("title", ""), "request": j.get("request", ""),
                           "depends_on": j.get("depends_on"), "anchors": j.get("anchors", [])}
                          for j in jobs],
                    summary=prop.get("summary", ""),
                )
                pid = created["proposal_id"]
                # Merge planner's per-job verification annotations onto the stored
                # cards for the UI (held-back reason, confidence, per-anchor
                # relevance verdicts). Existence decides held_back; relevance is
                # advisory only.
                merged = dict(created)
                for stored, raw in zip(merged.get("jobs", []), jobs):
                    stored["held_back"] = bool(raw.get("held_back"))
                    stored["held_back_reason"] = raw.get("held_back_reason")
                    stored["confirmed"] = bool(raw.get("confirmed"))
                    stored["confidence"] = raw.get("confidence")
                    stored["anchors"] = raw.get("anchors", stored.get("anchors", []))
                _ask_emit(tid, {"type": "proposal", "proposal": merged,
                                "held_back": prop.get("held_back", []),
                                "raw_count": prop.get("raw_count", 0)})
                try:
                    _channels.transcript_append(cid, tid, {"role": "draft", "proposal_id": pid})
                except Exception:
                    pass
            except Exception as exc:
                _ask_emit(tid, {"type": "error", "error": str(exc)})
            finally:
                _ask_finish(tid)

        threading.Thread(target=_run, daemon=True).start()
        self._stream_thread_buffer(tid, send)

    def _transcript_text(self, cid: str, tid: str) -> str:
        """Flatten the thread transcript to plain text for the derivation agent."""
        lines = []
        for e in _channels.transcript_read(cid, tid):
            role = e.get("role", "")
            if role in ("user", "assistant"):
                lines.append(f"{role.upper()}: {e.get('text', '')}")
        return "\n\n".join(lines)

    def _channel_submit(self, cid: str, tid: str) -> None:
        """POST /api/channel/<cid>/<tid>/submit {proposal_id, included_seqs} —
        walk jobs in seq order calling submit_job(after=<resolved parent>). The
        depends_on index → real job_id remap is the ONLY new queue logic. Only
        job['request'] carries forward (job-isolation rule)."""
        body = self._read_body()
        pid = str(body.get("proposal_id", "")).strip()
        if not pid:
            self._send_json({"ok": False, "error": "proposal_id required"}, 400)
            return
        try:
            channel = _channels.channel_load(cid)
            prop = _channels.proposal_load(cid, pid)
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, 404)
            return

        repo = channel["repo"]
        priority = int(body.get("priority", 0) or 0)
        model_hint = str(body.get("model_hint", "auto")).strip() or "auto"
        included = body.get("included_seqs")
        if isinstance(included, list):
            include_set = {int(s) for s in included}
        else:
            include_set = None  # all

        jobs = sorted(prop.get("jobs", []), key=lambda j: j.get("seq", 0))
        # seq (0-based proposal index) → real job_id, for the depends_on remap.
        seq_to_job: dict[int, str] = {}
        results = []
        try:
            for j in jobs:
                seq = int(j.get("seq", 0))
                if include_set is not None and seq not in include_set:
                    continue
                request = str(j.get("request", "")).strip()
                if not request:
                    continue
                dep = j.get("depends_on")
                after = None
                if isinstance(dep, int) and not isinstance(dep, bool):
                    after = seq_to_job.get(dep)  # parent already queued earlier
                # ONLY the request string crosses into the job — no thread,
                # transcript, channel, or proposal reference is ever written.
                job_id, job_name = submit_job(request, repo, priority, model_hint, after)
                seq_to_job[seq] = job_id
                results.append({"seq": seq, "job_id": job_id, "name": job_name})
        except (ValueError, RuntimeError) as exc:
            self._send_json({"ok": False, "error": str(exc)}, 400)
            return

        try:
            _channels.proposal_update(cid, pid, status="submitted",
                                      submitted_job_ids=[r["job_id"] for r in results])
            _channels.transcript_append(cid, tid, {
                "role": "submitted", "proposal_id": pid,
                "jobs": results,
            })
        except Exception:
            pass
        self._send_json({"ok": True, "jobs": results})


# ── Entry point ────────────────────────────────────────────────────────────────

PID_FILE       = AGENTIC_HOME / "serve.pid"

# Run mode + target come from settings.json (the UI), not env vars — so you can
# launch the server once and choose local/cloud and the project in the browser.
# Resolved live each call so flipping mode in the panel takes effect without a
# restart (the worker spawn re-resolves too — see _run_worker).
def _mode() -> str:
    return _settings.load().get("mode", "local")

def is_local() -> bool:
    return _mode() == "local"

def local_model() -> str:
    return _settings.load().get("local_model", "qwen-coder:latest")

def default_repo() -> str:
    return _settings.load().get("default_repo") or os.getcwd()

def browse_root() -> str:
    """Root the directory picker may browse. In Docker this is the broad host
    dir bind-mounted at its identity path (compose sets BROWSE_ROOT, default the
    host home). On a native install it defaults to the user's home dir. The
    /api/browse endpoint confines all listing to this root — no traversal out."""
    root = os.environ.get("BROWSE_ROOT", "").strip() or str(Path.home())
    try:
        return str(Path(root).resolve())
    except Exception:
        return str(Path.home())

class _Server(http.server.ThreadingHTTPServer):
    def handle_error(self, request: Any, client_address: Any) -> None:
        # Swallow disconnects — these are normal when browsers close SSE streams
        # or cancel requests; they are not actionable errors.
        exc_type = sys.exc_info()[0]
        if exc_type is not None and issubclass(exc_type, (ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 4080
    queue_init()

    PID_FILE.write_text(f"{os.getpid()}:{port}")
    atexit.register(lambda: PID_FILE.unlink(missing_ok=True))
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    # Bind localhost by default (bare-metal: don't expose on the network). The
    # container entrypoint sets AGENTIC_BIND=0.0.0.0 so the Docker port mapping
    # can reach it.
    bind = os.environ.get("AGENTIC_BIND", "127.0.0.1")
    server = _Server((bind, port), Handler)
    print(f"agentic dashboard → http://localhost:{port}")
    print("agentic serve stop  — to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
