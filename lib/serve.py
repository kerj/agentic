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
from typing import Any

from job_queue import (
    AGENTIC_HOME, STATES,
    queue_init, submit_job, find_job, read_jobs,
    cancel_job, accept_job, accept_chain, abandon_job,
    set_chain, set_job_status, reject_job, delete_job, review_job, submit_review_job,
    get_diff, get_agent_activity, fetch_models, get_ollama_models,
    get_job_chain, get_job_detail, get_job_full, get_repos,
)

# ── Worker state (global, guarded by _worker_lock) ────────────────────────────

_worker_lock    = threading.Lock()
_worker_running = False
_worker_proc: "subprocess.Popen[str] | None" = None

# Line buffer + condition so reconnecting SSE clients can catch up.
# Primary stream appends here; waiters wake on _worker_cond.notify_all().
_worker_log: list[str] = []
_worker_rc:  int | None = None
_worker_cond = threading.Condition(_worker_lock)

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
.btn-amber  { background: #3a1f00; color: #e8943a; border: 1px solid #7a4010; }
.btn-amber:hover { border-color: #e8943a; }
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
.dot-merged    { background: #8957e5; }
.job-body { flex: 1; min-width: 0; cursor: pointer; }
.job-top { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 2px; }
.job-name { font-size: 13px; font-weight: 600; color: #e6edf3; }
.job-id-sub { margin-bottom: 4px; }
.job-id { font-family: "SFMono-Regular", Consolas, monospace; font-size: 11px; color: #6e7681; }
a.job-id { color: #6e7681; text-decoration: none; }
a.job-id:hover { color: #388bfd; text-decoration: underline; }
.state-badge { font-size: 11px; font-weight: 600; padding: 2px 8px; border-radius: 12px; border: 1px solid transparent; }
.badge-pending   { color: #388bfd; border-color: #1f4391; background: #0d1f42; }
.badge-running   { color: #f0883e; border-color: #6d3d1a; background: #2a1900; }
.badge-done      { color: #3fb950; border-color: #1e4a26; background: #0a2614; }
.badge-failed    { color: #f85149; border-color: #6d2120; background: #2c0b0b; }
.badge-abandoned { color: #d29922; border-color: #5a3e1b; background: #2a1f00; }
.badge-cancelled { color: #6e7681; border-color: #30363d; background: #161b22; }
.badge-merged    { color: #8957e5; border-color: #3d2364; background: #1a0e2e; }
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
.chain-review > .job-card { border-left: 3px solid #388bfd; }
.badge-review-type { display: inline-block; font-size: 10px; padding: 1px 6px; border-radius: 4px; color: #79c0ff; border: 1px solid #1f4391; background: #0d1f42; margin-left: 6px; vertical-align: middle; }
.badge-profile { display: inline-block; font-size: 10px; padding: 1px 6px; border-radius: 4px; color: #7ee787; border: 1px solid #1a4a26; background: #0a1f12; margin-left: 6px; vertical-align: middle; }

/* Status popover menu */
.status-menu { position: relative; display: inline-block; }
.status-menu-btn { background: transparent; border: 1px solid #30363d; border-radius: 4px; color: #8b949e; cursor: pointer; padding: 3px 7px; font-size: 12px; }
.status-menu-btn:hover { border-color: #8b949e; color: #e6edf3; }
.status-dropdown { display: none; position: absolute; right: 0; top: 100%; margin-top: 4px; background: #161b22; border: 1px solid #30363d; border-radius: 6px; min-width: 140px; z-index: 50; box-shadow: 0 8px 24px rgba(0,0,0,.4); }
.status-dropdown.open { display: block; }
.status-dropdown button { display: block; width: 100%; text-align: left; padding: 8px 12px; background: none; border: none; color: #e6edf3; cursor: pointer; font-size: 13px; }
.status-dropdown button:hover { background: #21262d; }
.status-dropdown .divider { border-top: 1px solid #21262d; margin: 4px 0; }
.status-dropdown button.menu-danger { color: #f85149; }
.status-dropdown button.menu-danger:hover { background: #2d1a1a; }

/* Log panel — slides up from bottom */
#log-panel {
  position: fixed; bottom: 0; left: 0; right: 0; height: var(--log-h, 300px);
  background: #0d1117; border-top: 2px solid #30363d;
  transform: translateY(100%); transition: transform .25s ease;
  z-index: 200; display: flex; flex-direction: column;
}
#log-panel.open { transform: translateY(0); }
#log-resize { height: 6px; cursor: ns-resize; background: #30363d; flex-shrink: 0; }
#log-resize:hover { background: #484f58; }
#log-header {
  display: flex; align-items: center; gap: 8px;
  padding: 8px 16px; border-bottom: 1px solid #21262d; flex-shrink: 0;
}
#log-header span { font-size: 13px; font-weight: 600; }
#log-close { margin-left: auto; background: none; border: none; color: #8b949e; cursor: pointer; font-size: 18px; line-height: 1; padding: 0 4px; }
#log-close:hover { color: #e6edf3; }
#log-collapse { background: none; border: none; color: #8b949e; cursor: pointer; font-size: 14px; line-height: 1; padding: 0 4px; }
#log-collapse:hover { color: #e6edf3; }
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
  width: 100%; max-width: 1200px; display: flex; flex-direction: column;
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
.sd-table { width: 100%; border-collapse: collapse; font-family: "SFMono-Regular", Consolas, monospace; font-size: 11px; line-height: 1.45; }
.sd-hunk-row td { background: #1c2d3a; color: #388bfd; padding: 2px 8px; font-size: 11px; }
.sd-ln { width: 44px; min-width: 44px; padding: 1px 8px; text-align: right; color: #6e7681; user-select: none; border-right: 1px solid #21262d; white-space: nowrap; vertical-align: top; }
.sd-cell { padding: 1px 8px; white-space: pre; overflow: hidden; width: 50%; vertical-align: top; }
.sd-div { width: 1px; min-width: 1px; background: #30363d; padding: 0; }
.sd-a { background: #0a2614; }
.sd-d { background: #2c0b0b; }
.sd-e { background: #0d1117; }
.sd-ln[data-line] { cursor: pointer; }
.sd-ln[data-line]:hover { background: #1c2d3a; color: #e6edf3; }
.sd-ln.ln-anchor { background: #1c2d3a; color: #388bfd; }
#review-panel { border-top: 1px solid #30363d; flex-shrink: 0; max-height: 320px; overflow-y: auto; }
#review-composer { padding: 10px 14px; display: none; border-bottom: 1px solid #21262d; }
#review-composer-label { font-size: 11px; color: #8b949e; margin-bottom: 6px; font-family: monospace; }
#review-textarea { width: 100%; box-sizing: border-box; background: #0d1117; border: 1px solid #30363d; border-radius: 6px; color: #e6edf3; padding: 8px; font-family: inherit; font-size: 12px; resize: vertical; }
#review-textarea:focus { outline: none; border-color: #388bfd; }
.review-actions { display: flex; gap: 6px; justify-content: flex-end; margin-top: 6px; }
.review-comment { display: flex; align-items: flex-start; gap: 8px; padding: 7px 14px; border-bottom: 1px solid #21262d; font-size: 12px; }
.review-comment-loc { font-family: monospace; font-size: 11px; color: #8b949e; flex-shrink: 0; padding-top: 1px; }
.review-comment-text { color: #e6edf3; flex: 1; white-space: pre-wrap; word-break: break-word; }
.review-comment-del { background: none; border: none; color: #6e7681; cursor: pointer; font-size: 16px; padding: 0; flex-shrink: 0; line-height: 1; }
.review-comment-del:hover { color: #f85149; }
#review-submit-row { padding: 8px 14px; display: none; align-items: center; gap: 10px; justify-content: flex-end; border-top: 1px solid #21262d; }
#review-count { font-size: 12px; color: #8b949e; }
.sd-ln.ln-commented { background: #0d1f38 !important; border-left: 3px solid #388bfd !important; padding-left: 5px; color: #58a6ff !important; }

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
  __LOCAL_BADGE__
  <button id="run-btn" onclick="runWorker(false)">▶ Run Worker</button>
  <button id="run-all-btn" onclick="runWorker(true)" style="background:#1f4391;color:#88b4ff;padding:6px 14px;border-radius:6px;border:none;cursor:pointer;font-size:13px;font-weight:500;margin-left:4px;">▶▶ Run All</button>
  <button id="stop-btn" onclick="stopWorker()" style="display:none;background:#6d2120;color:#f47067;padding:6px 14px;border-radius:6px;border:1px solid #6d2120;cursor:pointer;font-size:13px;font-weight:500;margin-left:4px;">■ Stop</button>
  <span id="age">loading…</span>
</header>

<div class="container">
  <div class="card">
    <h2>Submit Job</h2>
    <select id="repo-select" style="width:100%;margin-bottom:8px;font-family:monospace;font-size:12px" onchange="onRepoChange(this.value)">
      <option value="__DEFAULT_REPO_HTML__">__DEFAULT_REPO_HTML__</option>
    </select>
    <textarea id="req" placeholder="Describe the change you want…" rows="3"></textarea>
    <div class="form-row">
      <select id="priority">
        <option value="0">Priority 0 — normal</option>
        <option value="1">Priority 1 — high</option>
        <option value="5">Priority 5 — urgent</option>
      </select>
      __MODEL_FIELD__
      <button class="btn btn-green" onclick="submitJob()">Submit</button>
    </div>
    <div class="form-row" style="margin-top:6px">
      <select id="after" style="flex:1">
        <option value="">— no chain (independent job) —</option>
      </select>
    </div>
    <div style="font-size:11px;color:#6e7681;margin-top:4px">Cmd+Enter / Ctrl+Enter to submit · Pick a job above to chain this one after it</div>
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

<script>
let currentFilter = 'all';
let searchQuery = '';
let allJobs = [];
let lastFetch = null;
let selectedRepo = __DEFAULT_REPO_JS__;
const IS_LOCAL = __IS_LOCAL__;

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
  const isReviewJob = j.job_type === 'review';
  if (s === 'done') {
    const _rd = _loadDraft(j.id);
    const _rcount = _rd ? (_rd.comments || []).length : 0;
    const _rlabel = _rcount ? `Review (${_rcount})` : 'Review';
    const _rcls   = (_rcount && !(_rd && _rd.submitted)) ? 'btn btn-amber' : 'btn btn-ghost';
    actions.push(`<button class="${_rcls}" onclick="${sp}viewDiff('${escHtml(j.id)}')">${_rlabel}</button>`);
  }
  if (s === 'failed' || s === 'merged')
    actions.push(`<button class="btn btn-ghost" onclick="${sp}viewDiff('${escHtml(j.id)}')">View Diff</button>`);
  if (s === 'done') {
    // Accept Chain only makes sense when there are manual chain children (non-review)
    const hasChainChild = allJobs.some(x => x.parent_request_id === j.id && x._state === 'done' && x.job_type !== 'review');
    if (hasChainChild)
      actions.push(`<button class="btn btn-blue" onclick="${sp}acceptChain('${escHtml(j.id)}')">Accept Chain ↓</button>`);
    actions.push(`<button class="btn btn-ghost" onclick="${sp}reviewJob('${escHtml(j.id)}')">Review in IDE</button>`);
    actions.push(`<button class="btn btn-blue" style="opacity:.7" onclick="${sp}acceptJob('${escHtml(j.id)}')">Accept</button>`);
  }
  if (s === 'done' || s === 'failed')
    actions.push(`<button class="btn btn-red" onclick="${sp}rejectJob('${escHtml(j.id)}')">Reject</button>`);

  // Status + chain menu — always present
  const allStatuses = ['pending','running','done','merged','failed','abandoned','cancelled'];
  const statusItems = allStatuses.filter(x => x !== s).map(st =>
    `<button onclick="event.stopPropagation();setStatus('${escHtml(j.id)}','${st}')">→ ${st}</button>`
  ).join('');
  const chainLabel = j.parent_request_id ? '🔗 Edit chain…' : '🔗 Set chain…';
  const deleteItem = s !== 'running'
    ? `<div class="divider"></div><button class="menu-danger" onclick="event.stopPropagation();deleteJob('${escHtml(j.id)}')">🗑 Delete…</button>`
    : '';
  const menuItems = `<button onclick="event.stopPropagation();openChain('${escHtml(j.id)}')">${chainLabel}</button><div class="divider"></div>${statusItems}${deleteItem}`;
  actions.push(`<div class="status-menu">
    <button class="status-menu-btn" onclick="event.stopPropagation();toggleMenu(this)">⋯</button>
    <div class="status-dropdown">${menuItems}</div>
  </div>`);

  return `
<div class="job-card">
  <div class="state-dot dot-${escHtml(s)}"></div>
  <div class="job-body" onclick="openDrawer('${escHtml(j.id)}')">
    <div class="job-top">
      <span class="job-name">${escHtml(j.name || j.id)}</span>${isReviewJob ? '<span class="badge-review-type">review</span>' : ''}${j.profile_display || j.profile ? `<span class="badge-profile">${escHtml(j.profile_display || j.profile)}</span>` : ''}
      <span class="state-badge badge-${escHtml(s)}">${escHtml(s)}</span>
    </div>
    <div class="job-id-sub"><a class="job-id" href="/job/${escHtml(j.id)}" onclick="event.stopPropagation()">${escHtml(j.id)}</a></div>
    <div class="job-request">${escHtml(j.request || '')}</div>
    <div class="job-meta">
      <span>⏱ ${relTime(j.submitted_at)}</span>
      <span>📁 ${escHtml(j.target_repo || '')}</span>
      <span>🤖 ${escHtml(j.model_hint || 'auto')}</span>
      ${j.priority   ? '<span>⬆ p' + escHtml(String(j.priority)) + '</span>' : ''}
    </div>
    ${j.summary ? `<div class="${sumCls}">↳ ${escHtml(j.summary)}</div>` : ''}
  </div>
  <div class="job-actions">${actions.join('')}</div>
</div>`;
}

function renderJobs() {
  if (document.querySelector('.status-dropdown.open, #chain-modal.open')) return;
  const list = document.getElementById('job-list');
  let jobs = currentFilter === 'all' ? allJobs : allJobs.filter(j => j._state === currentFilter);
  if (selectedRepo) jobs = jobs.filter(j => j.target_repo === selectedRepo);
  if (searchQuery) {
    jobs = jobs.filter(j =>
      (j.name || '').toLowerCase().includes(searchQuery) ||
      (j.request || '').toLowerCase().includes(searchQuery) ||
      (j.target_repo || '').toLowerCase().includes(searchQuery) ||
      (j.id || '').toLowerCase().includes(searchQuery)
    );
  }
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
    // Separate review children (same-branch) from manual chain children (own branch)
    const reviewChildren = children.filter(c => c.job_type === 'review');
    const chainChildren  = children.filter(c => c.job_type !== 'review');
    const reviewHtml = reviewChildren.map(c =>
      `<div class="chain-child chain-review">${renderChain(c, depth + 1)}</div>`
    ).join('');
    const chainHtml = chainChildren.map(c =>
      `<div class="chain-child">${renderChain(c, depth + 1)}</div>`
    ).join('');
    return cardHtml + reviewHtml + chainHtml;
  }

  // Identify root jobs: no parent, or parent not in current filtered list
  const rootJobs = jobs.filter(j => !j.parent_request_id || !jobById[j.parent_request_id]);
  list.innerHTML = rootJobs.map(j => renderChain(j, 0)).join('');
}

function updateCounts(jobs) {
  const c = {all:0,pending:0,running:0,done:0,merged:0,failed:0,abandoned:0,cancelled:0};
  jobs.forEach(j => { c.all++; if (c[j._state] !== undefined) c[j._state]++; });
  Object.keys(c).forEach(k => { const el = document.getElementById('cnt-'+k); if(el) el.textContent = c[k]; });
}

async function fetchRepos() {
  try {
    const r = await fetch('/api/repos');
    if (!r.ok) return;
    const repos = await r.json();
    const sel = document.getElementById('repo-select');
    const current = sel.value;
    sel.innerHTML = '';
    repos.forEach(repo => {
      const opt = document.createElement('option');
      opt.value = repo;
      opt.textContent = repo;
      if (repo === current) opt.selected = true;
      sel.appendChild(opt);
    });
    if (!sel.value && repos.length) sel.value = repos[0];
  } catch(e) {}
}

function onRepoChange(repo) {
  selectedRepo = repo;
  renderJobs();
  populateAfterDropdown();
}

async function fetchJobs() {
  try {
    const r = await fetch('/api/jobs');
    if (!r.ok) return;
    allJobs = await r.json();
    lastFetch = Date.now();
    updateCounts(allJobs);
    renderJobs();
    populateAfterDropdown();
  } catch(e) {}
}

function populateAfterDropdown() {
  const sel = document.getElementById('after');
  const current = sel.value;
  sel.innerHTML = '<option value="">— no chain (independent job) —</option>';
  allJobs.filter(j => !selectedRepo || j.target_repo === selectedRepo).forEach(j => {
    const opt = document.createElement('option');
    opt.value = j.id;
    const label = (j.name || j.id) + '  [' + j._state + ']  ' + (j.request || '').slice(0, 50);
    opt.textContent = label;
    if (j.id === current) opt.selected = true;
    sel.appendChild(opt);
  });
}

async function submitJob() {
  const request    = document.getElementById('req').value.trim();
  const repo       = selectedRepo || __DEFAULT_REPO_JS__;
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
    if (d.ok) { document.getElementById('req').value = ''; document.getElementById('after').value = ''; toast('Submitted ' + (d.name || d.id), 'success'); fetchJobs(); }
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

/* ── Review ── */
let reviewJobId = null;
let anchorLine = null;     // {file, line, side}
let reviewComments = [];
let reviewSubmitted = false;

function _loadDraft(jobId) {
  try {
    const raw = localStorage.getItem('agentic_review_' + jobId);
    return raw ? JSON.parse(raw) : null;
  } catch { return null; }
}
function _saveDraft(jobId, comments, submitted) {
  try {
    if (comments.length || submitted) {
      localStorage.setItem('agentic_review_' + jobId, JSON.stringify({ comments, submitted: !!submitted }));
    } else {
      localStorage.removeItem('agentic_review_' + jobId);
    }
  } catch {}
}

/* ── Chain editor ── */
let _chainJobId = null;

function openChain(id) {
  _chainJobId = id;
  const job = allJobs.find(j => j.id === id);
  const current = job && job.parent_request_id;
  const currentJob = current && allJobs.find(j => j.id === current);
  const currentLabel = currentJob ? (currentJob.name || current) : 'none (independent)';
  document.getElementById('chain-current-label').innerHTML =
    'Current parent: <span>' + escHtml(currentLabel) + '</span>';

  // Populate dropdown — all jobs except this one and its own descendants
  const sel = document.getElementById('chain-select');
  sel.innerHTML = '<option value="">— run independently (no parent) —</option>';
  allJobs.filter(j => j.id !== id).forEach(j => {
    const opt = document.createElement('option');
    opt.value = j.id;
    opt.textContent = (j.name || j.id) + '  [' + j._state + ']  ' + (j.request || '').slice(0, 50);
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
  if (d.ok) { const pJob = parentId && allJobs.find(j => j.id === parentId); toast(parentId ? 'Chained after ' + (pJob && pJob.name ? pJob.name : parentId) : 'Removed from chain', 'success'); closeChain(); fetchJobs(); }
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

async function deleteJob(id) {
  const job = allJobs.find(j => j.id === id);
  const label = (job && job.name) ? job.name : id;
  if (!confirm(`Permanently delete "${label}"?\n\nThis removes the job, its branch, worktree, diff, and log. There is no undo.`)) return;
  const r = await fetch('/api/delete', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})});
  const d = await r.json();
  if (d.ok) { toast('Deleted ' + label, 'success'); fetchJobs(); }
  else toast('Error: ' + (d.error || 'unknown'), 'error');
}

async function acceptChain(id) {
  const job = allJobs.find(j => j.id === id);
  const base = job ? (job.base_branch || 'base branch') : 'base branch';
  // Walk the full chain (all descendants in 'done' state)
  const doneChain = [];
  let cur = id;
  while (cur) {
    const next = allJobs.find(j => j.parent_request_id === cur && j._state === 'done');
    if (!next) break;
    doneChain.push(next);
    cur = next.id;
  }
  // Review jobs don't produce a separate merge (commits already on parent branch)
  const mergeJobs   = doneChain.filter(j => j.job_type !== 'review').length + 1;
  const reviewJobs  = doneChain.filter(j => j.job_type === 'review').length;
  const reviewNote  = reviewJobs ? ` (includes ${reviewJobs} review job(s) already baked in)` : '';
  if (!confirm(`Merge ${mergeJobs} job(s) into a new staging branch based on '${base}'${reviewNote}.\n\nThe staging branch will NOT be merged into your working branch automatically — run:\n  git merge <staging-branch>\n\nProceed?`)) return;
  const r = await fetch('/api/accept-chain', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})});
  const d = await r.json();
  if (d.ok) {
    toast(`${d.accepted.length} job(s) accepted → staging: ${d.staging_branch}  (run: git merge ${d.staging_branch})`, 'success');
    fetchJobs();
  } else toast('Error: ' + (d.error || 'unknown'), 'error');
}

async function acceptJob(id, acknowledge) {
  if (!acknowledge && !confirm('Merge agentic/' + id + ' into its base branch?')) return;
  const r = await fetch('/api/accept', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id, acknowledge: !!acknowledge})});
  const d = await r.json();
  if (d.ok) { toast('Accepted ' + id + ' → ' + (d.message || ''), 'success'); fetchJobs(); return; }
  if ((d.error || '').includes('notable action')) {
    if (confirm('⚠️ ' + d.error + '\\n\\nOpen the job to review the flagged actions. Merge anyway?')) {
      return acceptJob(id, true);
    }
    return;
  }
  toast('Accept failed: ' + (d.error || 'unknown'), 'error');
}

async function rejectJob(id) {
  if (!confirm('Discard agentic/' + id + ' and delete the worktree?')) return;
  const r = await fetch('/api/reject', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})});
  const d = await r.json();
  if (d.ok) { toast('Rejected ' + id, 'success'); fetchJobs(); }
  else toast('Reject failed: ' + (d.error || 'unknown'), 'error');
}

async function reviewJob(id) {
  const r = await fetch('/api/apply-to-tree', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})});
  const d = await r.json();
  if (d.ok) toast((d.message || 'Changes applied — review in your IDE, then commit.'), 'success');
  else toast('Review failed: ' + (d.error || 'unknown'), 'error');
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
  { const tk = document.getElementById('log-tokens'); if (tk) { tk.textContent = ''; tk.style.display = 'none'; } }

  workerEs = new EventSource('/api/worker-stream');
  workerEs.onmessage = e => {
    const msg = JSON.parse(e.data);
    // Live token counter — updates the header in place, never the log body.
    if (msg.progress) {
      const p = msg.progress;
      const k = n => n >= 1000 ? (n/1000).toFixed(1).replace(/\.0$/,'') + 'k' : String(n);
      const el = document.getElementById('log-tokens');
      let txt = '🔢 ' + (p.input||0).toLocaleString() + ' in / ' + (p.output||0).toLocaleString() + ' out';
      if (p.ctx_budget) txt += ' · ctx ' + k(p.ctx_used||0) + '/' + k(p.ctx_budget);
      el.textContent = txt;
      el.style.display = '';
      return;
    }
    if (msg.replayed) {
      appendLog('── reconnected — replaying missed output ──', '');
    }
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
        const _openId = window._lastClaimedJobId;
        window._lastClaimedJobId = null;
        setTimeout(() => openDrawer(_openId), 500);
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
    document.getElementById('run-all-btn').disabled = false;
    document.getElementById('stop-btn').style.display = 'none';
    appendLog('Stream disconnected — click ▶ Run Worker to reconnect if job is still running', 'error');
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

/* ── Log panel resize & collapse ── */
(function() {
  const panel = document.getElementById('log-panel');
  const handle = document.getElementById('log-resize');
  const collapseBtn = document.getElementById('log-collapse');
  const COLLAPSED_H = 36;
  const STORAGE_KEY = 'agentic_log_h';
  let _isCollapsed = false;

  function getStoredH() {
    return parseInt(localStorage.getItem(STORAGE_KEY) || '300', 10);
  }
  function setH(h) {
    panel.style.setProperty('--log-h', h + 'px');
  }

  // Restore saved height
  setH(getStoredH());

  handle.addEventListener('mousedown', function(e) {
    e.preventDefault();
    const startY = e.clientY;
    const startH = panel.getBoundingClientRect().height;
    function onMove(ev) {
      const newH = Math.max(80, startH - (ev.clientY - startY));
      setH(newH);
    }
    function onUp(ev) {
      const finalH = panel.getBoundingClientRect().height;
      if (finalH > COLLAPSED_H + 10) {
        localStorage.setItem(STORAGE_KEY, String(Math.round(finalH)));
        _isCollapsed = false;
      }
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
    }
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  });

  collapseBtn.addEventListener('click', function() {
    if (_isCollapsed) {
      setH(getStoredH());
      _isCollapsed = false;
      collapseBtn.textContent = '⌄';
    } else {
      setH(COLLAPSED_H);
      _isCollapsed = true;
      collapseBtn.textContent = '⌃';
    }
  });
})();

/* ── Diff helpers (duplicated from detail page — both are standalone documents) ── */
function parseSplitDiff(raw) {
  const files = [];
  let file = null, hunk = null;
  for (const line of raw.split('\n')) {
    if (line.startsWith('diff --git ')) {
      if (file) files.push(file);
      const m = line.match(/diff --git a\/(.*) b\/(.*)/);
      file = { name: m ? m[2] : line, hunks: [] }; hunk = null;
    } else if (file && line.startsWith('@@ ')) {
      hunk = { header: line, lines: [] }; file.hunks.push(hunk);
    } else if (hunk) {
      if      (line.startsWith('+') && !line.startsWith('+++')) hunk.lines.push({ t:'a', c:line.slice(1) });
      else if (line.startsWith('-') && !line.startsWith('---')) hunk.lines.push({ t:'d', c:line.slice(1) });
      else if (!line.startsWith('\\') && !line.startsWith('index ') && !line.startsWith('diff ')
            && !line.startsWith('---') && !line.startsWith('+++'))
        hunk.lines.push({ t:'c', c:line.slice(1) });
    }
  }
  if (file) files.push(file);
  return files;
}

function buildSplitRows(hunk) {
  const rows = [], pending = [];
  const m = hunk.header.match(/@@ -(\d+)(?:,\d+)? \+(\d+)/);
  let lo = m ? +m[1] : 1, ln = m ? +m[2] : 1;
  const flush = () => { while (pending.length) rows.push({ l:{n:lo++,c:pending.shift().c,t:'d'}, r:null }); };
  for (const line of hunk.lines) {
    if      (line.t === 'c') { flush(); rows.push({ l:{n:lo++,c:line.c,t:'c'}, r:{n:ln++,c:line.c,t:'c'} }); }
    else if (line.t === 'd') { pending.push(line); }
    else { pending.length ? rows.push({ l:{n:lo++,c:pending.shift().c,t:'d'}, r:{n:ln++,c:line.c,t:'a'} })
                          : rows.push({ l:null, r:{n:ln++,c:line.c,t:'a'} }); }
  }
  flush();
  return rows;
}

/* ── Review comment UI ── */
function initReview(id) {
  reviewJobId = id;
  anchorLine = null;
  const draft = _loadDraft(id);
  reviewComments = draft ? [...draft.comments] : [];
  reviewSubmitted = !!(draft && draft.submitted);
  document.getElementById('review-composer').style.display = 'none';
  renderReviewComments();
  const job = allJobs.find(j => j.id === id);
  document.getElementById('review-panel').style.display =
    (job && job._state === 'merged') ? 'none' : '';
}

function _clearAnchor() {
  anchorLine = null;
  document.querySelectorAll('.sd-ln.ln-anchor').forEach(el => el.classList.remove('ln-anchor'));
}

function _highlightRange(file, start, end, side) {
  document.querySelectorAll('.sd-ln[data-side]').forEach(el => {
    if (el.dataset.file === file && el.dataset.side === side) {
      const ln = +el.dataset.line;
      if (ln >= start && ln <= end) el.classList.add('ln-anchor');
    }
  });
}

function openComposer(file, startLine, endLine, side) {
  const loc = startLine === endLine ? `${file}:${startLine}` : `${file}:${startLine}–${endLine}`;
  document.getElementById('review-composer-label').textContent = loc;
  document.getElementById('review-textarea').value = '';
  const c = document.getElementById('review-composer');
  c.dataset.file = file; c.dataset.start = startLine; c.dataset.end = endLine; c.dataset.side = side;
  c.style.display = 'block';
  document.getElementById('review-textarea').focus();
}

function saveReviewComment() {
  const text = document.getElementById('review-textarea').value.trim();
  if (!text) return;
  const c = document.getElementById('review-composer');
  reviewComments.push({ file: c.dataset.file, startLine: +c.dataset.start, endLine: +c.dataset.end, side: c.dataset.side, comment: text });
  c.style.display = 'none';
  _clearAnchor();
  reviewSubmitted = false;
  if (reviewJobId) _saveDraft(reviewJobId, reviewComments, false);
  renderReviewComments();
  renderCommentMarkers();
  renderJobs();
}

function cancelReviewComment() {
  document.getElementById('review-composer').style.display = 'none';
  _clearAnchor();
}

function deleteReviewComment(idx) {
  reviewComments.splice(idx, 1);
  if (reviewJobId) _saveDraft(reviewJobId, reviewComments, false);
  renderReviewComments();
  renderCommentMarkers();
  renderJobs();
}

function renderCommentMarkers() {
  document.querySelectorAll('.sd-ln.ln-commented').forEach(el => el.classList.remove('ln-commented'));
  for (const c of reviewComments) {
    document.querySelectorAll('.sd-ln[data-side]').forEach(el => {
      if (el.dataset.file === c.file && el.dataset.side === c.side) {
        const ln = +el.dataset.line;
        if (ln >= c.startLine && ln <= c.endLine) el.classList.add('ln-commented');
      }
    });
  }
}

function renderReviewComments() {
  const list = document.getElementById('review-comments-list');
  const row  = document.getElementById('review-submit-row');
  if (!reviewComments.length) { list.innerHTML = ''; row.style.display = 'none'; return; }
  list.innerHTML = reviewComments.map((c, i) => {
    const loc = c.startLine === c.endLine ? `${c.file}:${c.startLine}` : `${c.file}:${c.startLine}–${c.endLine}`;
    const del = reviewSubmitted ? '' : `<button class="review-comment-del" onclick="deleteReviewComment(${i})" title="Remove">×</button>`;
    return `<div class="review-comment">
      <span class="review-comment-loc">${escHtml(loc)}</span>
      <span class="review-comment-text">${escHtml(c.comment)}</span>
      ${del}
    </div>`;
  }).join('');
  if (reviewSubmitted) {
    row.innerHTML = '<span style="color:#3fb950;font-size:12px">✓ Review submitted</span>';
  } else {
    const n = reviewComments.length;
    row.innerHTML = `<span id="review-count">${n} comment${n !== 1 ? 's' : ''}</span><button class="btn btn-primary" onclick="submitReview()">Submit Review</button>`;
  }
  row.style.display = 'flex';
}

async function submitReview() {
  if (!reviewComments.length || !reviewJobId) return;
  const btn = document.querySelector('#review-submit-row button');
  btn.disabled = true; btn.textContent = 'Submitting…';
  try {
    const r = await fetch('/api/review', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ job_id: reviewJobId, comments: reviewComments }),
    });
    const d = await r.json();
    if (d.ok) { _saveDraft(reviewJobId, reviewComments, true); reviewSubmitted = true; renderReviewComments(); renderJobs(); toast('Review submitted — ' + (d.name || d.id), 'success'); closeDiff(); }
    else { toast('Failed: ' + (d.error || 'unknown'), 'error'); btn.disabled = false; btn.textContent = 'Submit Review'; }
  } catch(e) { toast('Network error', 'error'); btn.disabled = false; btn.textContent = 'Submit Review'; }
}

document.getElementById('diff-content').addEventListener('click', e => {
  const td = e.target.closest('.sd-ln[data-line]');
  if (!td) { _clearAnchor(); return; }
  const file = td.dataset.file, line = +td.dataset.line, side = td.dataset.side;
  if (!anchorLine || anchorLine.file !== file) {
    _clearAnchor();
    anchorLine = { file, line, side };
    td.classList.add('ln-anchor');
  } else {
    const start = Math.min(anchorLine.line, line), end = Math.max(anchorLine.line, line);
    _clearAnchor();
    _highlightRange(file, start, end, side);
    openComposer(file, start, end, side);
  }
});

/* ── Diff modal ── */
async function viewDiff(id) {
  const job = allJobs.find(j => j.id === id);
  const label = (job && job.name) ? job.name : id;
  document.getElementById('diff-title').textContent = label;
  document.getElementById('diff-content').innerHTML = '<div style="padding:16px;color:#8b949e">Loading…</div>';
  document.getElementById('diff-modal').classList.add('open');
  initReview(id);
  try {
    const r = await fetch('/api/diff/' + encodeURIComponent(id));
    const d = await r.json();
    if (!d.ok) {
      document.getElementById('diff-content').innerHTML = '<div style="padding:16px;color:#f85149">' + escHtml(d.error) + '</div>';
      return;
    }
    const raw = d.diff || '';
    if (!raw.trim()) {
      document.getElementById('diff-content').innerHTML = '<div style="padding:16px;color:#6e7681">No diff available.</div>';
      return;
    }
    const files = parseSplitDiff(raw);
    if (!files.length) {
      document.getElementById('diff-content').innerHTML = '<div style="padding:16px;color:#6e7681">No changes found.</div>';
      return;
    }
    const html = files.map(fileObj => {
      let diffHtml = '<div style="padding:8px;font-size:11px;color:#6e7681">No hunks.</div>';
      if (fileObj.hunks.length) {
        const tbl = fileObj.hunks.map(h => {
          const dataRows = buildSplitRows(h).map(row => {
            const L=row.l, R=row.r, lt=L?L.t:'e', rt=R?R.t:'e';
            const fn = escHtml(fileObj.name);
            const lnL = L ? ` data-file="${fn}" data-line="${L.n}" data-side="L"` : '';
            const lnR = R ? ` data-file="${fn}" data-line="${R.n}" data-side="R"` : '';
            return `<tr>`
              + `<td class="sd-ln sd-${lt}"${lnL}>${L?L.n:''}</td>`
              + `<td class="sd-cell sd-${lt}">${L?escHtml(L.c):''}</td>`
              + `<td class="sd-div"></td>`
              + `<td class="sd-ln sd-${rt}"${lnR}>${R?R.n:''}</td>`
              + `<td class="sd-cell sd-${rt}">${R?escHtml(R.c):''}</td>`
              + `</tr>`;
          }).join('');
          return `<tr class="sd-hunk-row"><td colspan="5">${escHtml(h.header)}</td></tr>${dataRows}`;
        }).join('');
        diffHtml = `<div style="overflow-x:auto"><table class="sd-table">${tbl}</table></div>`;
      }
      return `<details open style="border:1px solid #21262d;border-radius:6px;margin-bottom:6px;overflow:hidden">
        <summary style="display:flex;align-items:center;gap:8px;cursor:pointer;list-style:none;padding:6px 10px;background:#161b22">
          <span style="color:#3fb950;font-size:13px">±</span>
          <span style="font-family:monospace;font-size:12px;color:#e6edf3">${escHtml(fileObj.name)}</span>
        </summary>
        ${diffHtml}
      </details>`;
    }).join('');
    document.getElementById('diff-content').innerHTML = `<div style="padding:12px">${html}</div>`;
    renderCommentMarkers();
  } catch(e) {
    document.getElementById('diff-content').innerHTML = '<div style="padding:16px;color:#f85149">Failed to load diff</div>';
  }
}

function closeDiff(e) {
  if (!e || e.target === document.getElementById('diff-modal') || e.currentTarget === document.getElementById('diff-close')) {
    if (reviewJobId) _saveDraft(reviewJobId, reviewComments, reviewSubmitted);
    document.getElementById('diff-modal').classList.remove('open');
    _clearAnchor();
    reviewComments = [];
    document.getElementById('review-composer').style.display = 'none';
    renderReviewComments();
    renderJobs();
  }
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

// Populate model selector
if (IS_LOCAL) {
  fetch('/api/ollama-models').then(r => r.json()).then(models => {
    const sel = document.getElementById('model');
    if (!sel || sel.tagName !== 'SELECT') return;
    sel.innerHTML = models.length
      ? models.map(m => `<option value="${escHtml(m)}">${escHtml(m)}</option>`).join('')
      : '<option value="">no models found</option>';
  }).catch(() => {});
} else {
  fetch('/api/models').then(r => r.json()).then(models => {
    const sel = document.getElementById('model');
    if (!sel || sel.tagName !== 'SELECT') return;
    const current = sel.dataset.current || '';
    sel.innerHTML = models.length
      ? models.map(m => `<option value="${escHtml(m)}"${m === current ? ' selected' : ''}>${escHtml(m)}</option>`).join('')
      : '<option value="">no models found</option>';
  }).catch(() => {});
}

fetchRepos();
fetchJobs();
// Auto-attach log stream if a worker is already running when the page loads
// (handles page refresh mid-job without requiring a manual button click)
fetch('/api/worker-status').then(r => r.json()).then(d => {
  if (d.running && !workerEs) runWorker();
}).catch(() => {});
setInterval(fetchJobs, 3000);
setInterval(fetchRepos, 30000);
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
/* Split diff */
.sd-file { margin-bottom: 10px; border: 1px solid #30363d; border-radius: 6px; overflow: hidden; }
.sd-file-hdr { background: #161b22; color: #e6edf3; padding: 6px 12px; font-size: 12px; font-family: "SFMono-Regular", Consolas, monospace; border-bottom: 1px solid #30363d; }
.sd-table { width: 100%; border-collapse: collapse; font-family: "SFMono-Regular", Consolas, monospace; font-size: 11px; line-height: 1.45; }
.sd-hunk-row td { background: #1c2d3a; color: #388bfd; padding: 2px 8px; font-size: 11px; }
.sd-ln { width: 44px; min-width: 44px; padding: 1px 8px; text-align: right; color: #6e7681; user-select: none; border-right: 1px solid #21262d; white-space: nowrap; vertical-align: top; }
.sd-cell { padding: 1px 8px; white-space: pre; overflow: hidden; width: 50%; vertical-align: top; }
.sd-div { width: 1px; min-width: 1px; background: #30363d; padding: 0; }
.sd-a { background: #0a2614; }
.sd-d { background: #2c0b0b; }
.sd-e { background: #0d1117; }
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
/* Chain visualizer */
.chain-flow { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.chain-chip { display: inline-block; padding: 4px 10px; border-radius: 12px; font-family: "SFMono-Regular", Consolas, monospace; font-size: 12px; border: 1px solid #30363d; background: #161b22; color: #8b949e; cursor: pointer; text-decoration: none; }
.chain-chip:hover { border-color: #8b949e; color: #e6edf3; }
.chain-chip.current { border-color: #388bfd; background: #0d1f42; color: #388bfd; cursor: default; }
.chain-arrow { color: #30363d; font-size: 14px; }
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

function parseSplitDiff(raw) {
  const files = [];
  let file = null, hunk = null;
  for (const line of raw.split('\n')) {
    if (line.startsWith('diff --git ')) {
      if (file) files.push(file);
      const m = line.match(/diff --git a\/(.*) b\/(.*)/);
      file = { name: m ? m[2] : line, hunks: [] }; hunk = null;
    } else if (file && line.startsWith('@@ ')) {
      hunk = { header: line, lines: [] }; file.hunks.push(hunk);
    } else if (hunk) {
      if      (line.startsWith('+') && !line.startsWith('+++')) hunk.lines.push({ t:'a', c:line.slice(1) });
      else if (line.startsWith('-') && !line.startsWith('---')) hunk.lines.push({ t:'d', c:line.slice(1) });
      else if (!line.startsWith('\\') && !line.startsWith('index ') && !line.startsWith('diff ')
            && !line.startsWith('---') && !line.startsWith('+++'))
        hunk.lines.push({ t:'c', c:line.slice(1) });
    }
  }
  if (file) files.push(file);
  return files;
}

function buildSplitRows(hunk) {
  const rows = [], pending = [];
  const m = hunk.header.match(/@@ -(\d+)(?:,\d+)? \+(\d+)/);
  let lo = m ? +m[1] : 1, ln = m ? +m[2] : 1;
  const flush = () => { while (pending.length) rows.push({ l:{n:lo++,c:pending.shift().c,t:'d'}, r:null }); };
  for (const line of hunk.lines) {
    if      (line.t === 'c') { flush(); rows.push({ l:{n:lo++,c:line.c,t:'c'}, r:{n:ln++,c:line.c,t:'c'} }); }
    else if (line.t === 'd') { pending.push(line); }
    else { pending.length ? rows.push({ l:{n:lo++,c:pending.shift().c,t:'d'}, r:{n:ln++,c:line.c,t:'a'} })
                          : rows.push({ l:null, r:{n:ln++,c:line.c,t:'a'} }); }
  }
  flush();
  return rows;
}

function estimateCost(model, inputTokens, outputTokens) {
  const r = {haiku:[0.80,4], sonnet:[3,15], opus:[15,75]};
  const k = Object.keys(r).find(k => (model||'').toLowerCase().includes(k));
  if (!k) return null;
  const cost = (inputTokens * r[k][0] + outputTokens * r[k][1]) / 1e6;
  return cost < 0.01 ? '<$0.01' : '$' + cost.toFixed(2);
}

function relTime(iso) {
  if (!iso) return '';
  const d = Math.floor((Date.now() - new Date(iso)) / 1000);
  if (d < 60)    return d + 's ago';
  if (d < 3600)  return Math.floor(d/60) + 'm ago';
  if (d < 86400) return Math.floor(d/3600) + 'h ago';
  return Math.floor(d/86400) + 'd ago';
}

async function acceptJob(id, acknowledge) {
  if (!acknowledge && !confirm('Merge agentic/' + id + ' into the target repo\'s current branch?')) return;
  const r = await fetch('/api/accept', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id, acknowledge: !!acknowledge})});
  const d = await r.json();
  if (d.ok) { toast('Accepted ' + id, 'success'); setTimeout(() => { _notifyParent(); }, 1200); return; }
  // Flagged job: accept_job refused pending acknowledgement. Surface the reason
  // and let the reviewer explicitly acknowledge after inspecting the banner above.
  if ((d.error || '').includes('notable action')) {
    if (confirm('⚠️ ' + d.error + '\\n\\nYou have reviewed the flagged actions in the Agent Activity panel and want to merge anyway?')) {
      return acceptJob(id, true);
    }
    return;
  }
  toast('Accept failed: ' + (d.error || 'unknown'), 'error');
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

async function reviewJob(id) {
  const r = await fetch('/api/apply-to-tree', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})});
  const d = await r.json();
  if (d.ok) toast((d.message || 'Changes applied — review in your IDE, then commit.'), 'success');
  else toast('Review failed: ' + (d.error || 'unknown'), 'error');
}

/* ── Diff cache — loaded once per page load for done/failed jobs ── */
let _diffCache = null;

async function loadDiff(state) {
  if (_diffCache !== null) return _diffCache;
  if (state !== 'done' && state !== 'failed') return '';
  try {
    const r = await fetch('/api/diff/' + encodeURIComponent(JOB_ID));
    const d = await r.json();
    _diffCache = (d.ok && d.diff) ? d.diff : '';
  } catch(e) {
    _diffCache = '';
  }
  return _diffCache;
}

function renderPage(job, activity, chain, diff) {
  const s = job._state || 'pending';
  const displayName = job.name || job.id;
  document.title = 'agentic — ' + displayName;
  document.getElementById('job-id-title').textContent = displayName;
  document.getElementById('state-badge-header').innerHTML =
    `<span class="state-badge badge-${escHtml(s)}">${escHtml(s)}</span>`;

  const actions = [];
  if (s === 'running')
    actions.push(`<button class="btn btn-red" onclick="abandonJob('${escHtml(job.id)}')">Abandon</button>`);
  if (s === 'done') {
    actions.push(`<button class="btn btn-ghost" onclick="reviewJob('${escHtml(job.id)}')">Review in IDE</button>`);
    actions.push(`<button class="btn btn-blue" onclick="acceptJob('${escHtml(job.id)}')">Accept</button>`);
  }
  if (s === 'done' || s === 'failed')
    actions.push(`<button class="btn btn-red" onclick="rejectJob('${escHtml(job.id)}')">Reject</button>`);
  document.getElementById('header-actions').innerHTML = actions.join('');

  let html = '';

  // ── 1. Agent Activity ──
  if (activity && activity.available) {
    const act = activity;
    html += `<div class="card" id="activity-card"><h2>Agent Activity</h2>`;

    // ── Anomaly banner: prominent "something's fishy" indicator ──
    if (act.is_flagged && (act.risk_flags||[]).length) {
      const labels = {network:'🌐 network', exfil:'🌐 network', destructive:'💥 destructive', sensitive_read:'🔑 secret read', oob:'📤 out-of-project access', oob_write:'📤 out-of-project write'};
      const seen = [...new Set(act.risk_flags.map(f => labels[f.risk_class] || f.risk_class))];
      html += `<div style="margin-bottom:16px;padding:12px 14px;background:#3d1416;border:1px solid #f85149;border-radius:6px">
        <div style="font-size:13px;font-weight:700;color:#ff7b72;display:flex;align-items:center;gap:8px">
          ⚠️ ${act.risk_flags.length} notable action(s) flagged — review before merging</div>
        <div style="font-size:11px;color:#f0a0a0;margin-top:6px">This agent took actions that can indicate a prompt-injection hijack: ${seen.join(', ')}. Inspect them below; merging requires acknowledgement.</div>
        <div style="margin-top:8px">`;
      act.risk_flags.forEach(f => {
        html += `<div style="font-size:11px;color:#e6edf3;padding:4px 8px;background:#010409;border-radius:4px;margin-top:4px;font-family:monospace">
          <span style="color:#ff7b72">[${escHtml(f.risk_class)}]</span> ${escHtml(f.tool)}: ${escHtml((f.detail||'').slice(0,160))}</div>`;
      });
      html += `</div></div>`;
    }

    // Stats row
    html += `<div style="display:flex;gap:16px;flex-wrap:wrap;margin-bottom:16px;padding:12px;background:#010409;border-radius:6px;border:1px solid #21262d">`;
    html += `<div style="text-align:center"><div style="font-size:20px;font-weight:600;color:#e6edf3">${(act.files_modified||[]).length}</div><div style="font-size:11px;color:#8b949e">files changed</div></div>`;
    html += `<div style="text-align:center"><div style="font-size:20px;font-weight:600;color:#e6edf3">${(act.tool_calls||[]).length}</div><div style="font-size:11px;color:#8b949e">tool calls</div></div>`;
    if (act.build_result) {
      const bc = act.build_result === 'passed' ? '#3fb950' : '#f85149';
      const bi = act.build_result === 'passed' ? '✓' : '✗';
      html += `<div style="text-align:center"><div style="font-size:20px;font-weight:600;color:${bc}">${bi}</div><div style="font-size:11px;color:#8b949e">build</div></div>`;
    }
    if (act.lint_result) {
      const lc = act.lint_result === 'passed' ? '#3fb950' : '#f85149';
      const li = act.lint_result === 'passed' ? '✓' : '✗';
      html += `<div style="text-align:center"><div style="font-size:20px;font-weight:600;color:${lc}">${li}</div><div style="font-size:11px;color:#8b949e">lint</div></div>`;
    }
    if (act.total_tokens) {
      html += `<div style="text-align:center"><div style="font-size:20px;font-weight:600;color:#e6edf3">${(act.total_tokens/1000).toFixed(1)}k</div><div style="font-size:11px;color:#8b949e">tokens</div></div>`;
    }
    const estCost = estimateCost(job.model_hint || '', act.input_tokens || 0, act.output_tokens || 0);
    if (estCost !== null) {
      html += `<div style="text-align:center"><div style="font-size:20px;font-weight:600;color:#e6edf3">${escHtml(estCost)}</div><div style="font-size:11px;color:#8b949e">est. cost</div></div>`;
    }
    html += `</div>`;

    // Phase summary
    const readCount   = (act.tool_calls||[]).filter(tc => tc.name === 'Read').length;
    const editCount   = (act.tool_calls||[]).filter(tc => tc.name === 'Edit' || tc.name === 'Write').length;
    const buildOk     = act.build_result === 'passed' ? '✓' : act.build_result === 'failed' ? '✗' : '—';
    const buildColor  = act.build_result === 'passed' ? '#3fb950' : act.build_result === 'failed' ? '#f85149' : '#6e7681';
    const hasCommit   = (act.tool_calls||[]).some(tc => tc.name === 'Bash' && (tc.input.command||'').includes('git commit'));
    const commitMark  = hasCommit ? '✓' : '—';
    const commitColor = hasCommit ? '#3fb950' : '#6e7681';
    html += `<div style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:16px;padding:10px 12px;background:#010409;border-radius:6px;border:1px solid #21262d;font-size:12px">
      <span style="color:#8b949e">📖 <strong style="color:#e6edf3">${readCount}</strong> reads</span>
      <span style="color:#8b949e">✏️ <strong style="color:#e6edf3">${editCount}</strong> edits</span>
      <span style="color:#8b949e">🔨 build <strong style="color:${buildColor}">${buildOk}</strong></span>
      <span style="color:#8b949e">📦 committed <strong style="color:${commitColor}">${commitMark}</strong></span>
    </div>`;

    // Files modified (split diff per file) — diff is already loaded
    const buildErrFiles = new Set(act.build_error_files || []);
    if ((act.files_modified||[]).length) {
      const byFile = {};
      if (diff) parseSplitDiff(diff).forEach(f => { byFile[f.name] = f; });
      const norm = p => p.replace(/^\.\//, '');
      html += `<div style="margin-bottom:14px">
        <div style="font-size:11px;font-weight:600;color:#8b949e;text-transform:uppercase;letter-spacing:.05em;margin-bottom:8px">Files Modified</div>`;
      act.files_modified.forEach(f => {
        const hasBuildErr = buildErrFiles.has(f);
        const nf = norm(f);
        const fileObj = byFile[nf] || byFile[f]
          || Object.values(byFile).find(o => norm(o.name) === nf || o.name.endsWith('/'+nf) || nf.endsWith('/'+norm(o.name)));
        let diffHtml = diff
          ? '<div style="padding:8px;font-size:11px;color:#6e7681">Diff not available for this file.</div>'
          : '<div style="padding:8px;font-size:11px;color:#6e7681">Diff not yet available (job still running).</div>';
        if (fileObj && fileObj.hunks.length) {
          const tbl = fileObj.hunks.map(h => {
            const dataRows = buildSplitRows(h).map(row => {
              const L=row.l, R=row.r, lt=L?L.t:'e', rt=R?R.t:'e';
              return `<tr>`
                + `<td class="sd-ln sd-${lt}">${L?L.n:''}</td>`
                + `<td class="sd-cell sd-${lt}">${L?escHtml(L.c):''}</td>`
                + `<td class="sd-div"></td>`
                + `<td class="sd-ln sd-${rt}">${R?R.n:''}</td>`
                + `<td class="sd-cell sd-${rt}">${R?escHtml(R.c):''}</td>`
                + `</tr>`;
            }).join('');
            return `<tr class="sd-hunk-row"><td colspan="5">${escHtml(h.header)}</td></tr>${dataRows}`;
          }).join('');
          diffHtml = `<div data-scroll-key="diff:${escHtml(f)}" style="overflow-x:auto;max-height:420px;overflow-y:auto"><table class="sd-table">${tbl}</table></div>`;
        } else if (fileObj) {
          diffHtml = '<div style="padding:8px;font-size:11px;color:#6e7681">No changes in this file.</div>';
        }
        html += `<details data-key="diff:${escHtml(f)}" style="border:1px solid #21262d;border-radius:6px;margin-bottom:6px;overflow:hidden">
          <summary style="display:flex;align-items:center;gap:8px;cursor:pointer;list-style:none;padding:6px 10px;background:#161b22">
            <span style="color:#3fb950;font-size:13px">±</span>
            <span style="font-family:monospace;font-size:12px;color:#e6edf3">${escHtml(f)}</span>
            ${hasBuildErr?'<span style="color:#f85149;font-size:11px;font-weight:600" title="Build error in this file">✗ build error</span>':''}
          </summary>
          ${diffHtml}
        </details>`;
      });
      html += `</div>`;
    }

    // Files read
    if ((act.files_read||[]).length) {
      html += `<div style="margin-bottom:14px">
        <div style="font-size:11px;font-weight:600;color:#8b949e;text-transform:uppercase;letter-spacing:.05em;margin-bottom:6px">Files Read</div>
        <div style="display:flex;flex-wrap:wrap;gap:6px">`;
      act.files_read.forEach(f => {
        html += `<span style="font-family:monospace;font-size:11px;color:#6e7681;background:#161b22;border:1px solid #30363d;border-radius:4px;padding:2px 6px">${escHtml(f)}</span>`;
      });
      html += `</div></div>`;
    }

    // Commands run
    const cmds = (act.tool_calls||[]).filter(tc => tc.name === 'Bash');
    if (cmds.length) {
      html += `<div style="margin-bottom:14px">
        <div style="font-size:11px;font-weight:600;color:#8b949e;text-transform:uppercase;letter-spacing:.05em;margin-bottom:6px">Commands Run</div>`;
      cmds.forEach(tc => {
        const cmd = (tc.input.command||'').trim().slice(0, 150);
        const ok  = tc.success;
        const risk = tc.risk_class;
        const riskBadge = risk ? `<span style="font-size:10px;font-weight:700;color:#fff;background:#da3633;padding:1px 6px;border-radius:8px;white-space:nowrap">⚠ ${escHtml(risk)}</span>` : '';
        html += `<div style="margin-bottom:6px">
          <div style="display:flex;align-items:center;gap:8px;padding:6px 10px;background:#010409;border-radius:4px;border-left:3px solid ${risk?'#da3633':(ok?'#3fb950':'#f85149')}">
            <span style="font-size:12px">${ok?'✓':'✗'}</span>
            <code style="font-size:12px;color:#e6edf3;word-break:break-all;flex:1">$ ${escHtml(cmd)}</code>
            ${riskBadge}
          </div>
          ${!ok && tc.output ? `<details data-key="cmd-err:${escHtml(cmd)}" style="margin-top:4px"><summary style="font-size:11px;color:#f85149;cursor:pointer;padding-left:10px">Show error output</summary><pre style="margin-top:4px;max-height:200px;overflow-y:auto;font-size:11px">${escHtml(tc.output.slice(0,2000))}</pre></details>` : ''}
        </div>`;
      });
      html += `</div>`;
    }

    // Agent reasoning
    if (act.assistant_text && act.assistant_text.trim().length > 50) {
      html += `<details data-key="agent-reasoning">
        <summary style="font-size:12px;color:#8b949e;cursor:pointer;user-select:none;padding:6px 0">
          Agent reasoning (${act.assistant_text.length.toLocaleString()} chars)
        </summary>
        <pre data-scroll-key="agent-reasoning" style="margin-top:8px;max-height:400px;overflow-y:auto;font-size:12px;white-space:pre-wrap">${escHtml(act.assistant_text.slice(0,8000))}</pre>
      </details>`;
    }

    // Token detail
    if (act.input_tokens || act.output_tokens) {
      html += `<div style="margin-top:12px;padding-top:12px;border-top:1px solid #21262d;font-size:12px;color:#6e7681">
        ${act.input_tokens.toLocaleString()} input + ${act.output_tokens.toLocaleString()} output = <strong style="color:#8b949e">${act.total_tokens.toLocaleString()}</strong> total tokens
      </div>`;
    }

    html += `</div>`; // close activity card
  }

  // ── 2. Request ──
  html += `<div class="card"><h2>Request</h2><pre>${escHtml(job.request || '')}</pre></div>`;

  // ── 3. State History ──
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

  // ── 4. Chain visualizer ──
  if (chain && chain.ok) {
    const hasParent = chain.parent !== null;
    const hasChildren = (chain.children || []).length > 0;
    if (hasParent || hasChildren) {
      html += `<div class="card"><h2>Job Chain</h2><div class="chain-flow">`;
      if (hasParent) {
        const p = chain.parent;
        html += `<a class="chain-chip" href="/job/${escHtml(p.id)}" title="${escHtml(p.request||'')}">↑ ${escHtml(p.name || p.id)}</a>`;
        html += `<span class="chain-arrow">→</span>`;
      }
      html += `<span class="chain-chip current">${escHtml(job.name || job.id)}</span>`;
      if (hasChildren) {
        chain.children.forEach(c => {
          html += `<span class="chain-arrow">→</span>`;
          html += `<a class="chain-chip" href="/job/${escHtml(c.id)}" title="${escHtml(c.request||'')}">↓ ${escHtml(c.name || c.id)}</a>`;
        });
      }
      html += `</div></div>`;
    }
  }

  // ── 5. Session tasks (legacy) ──
  const sess = job.session;
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
        ${outputText ? `<details data-key="task-out:${escHtml(String(task.id))}"><summary>View output (${lineCount} lines)</summary><pre>${escHtml(outputText)}</pre></details>` : ''}
      </div>`;
    });
    html += `</div>`;
  }

  // ── 6. Token Usage (legacy) ──
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

  // ── 7. Validation ──
  if (sess && (sess.validation_issues || sess.validation_warnings)) {
    html += `<div class="card"><h2>Validation</h2>`;
    if (sess.validation_issues)
      html += `<div class="validation-issues"><div class="validation-label">Issues</div><pre>${escHtml(sess.validation_issues)}</pre></div>`;
    if (sess.validation_warnings)
      html += `<div class="validation-warnings"><div class="validation-label">Warnings</div><pre>${escHtml(sess.validation_warnings)}</pre></div>`;
    html += `</div>`;
  }

  // ── 8. AI Review ──
  if (sess && sess.review) {
    html += `<div class="card"><h2>AI Review</h2><pre>${escHtml(sess.review)}</pre></div>`;
  }

  document.getElementById('main-content').innerHTML = html;
  scheduleRefresh(s);
}

function saveUIState() {
  const container = document.getElementById('main-content');
  const openDetails = Array.from(container.querySelectorAll('details[open]'))
    .map(el => el.dataset.key || el.querySelector('summary')?.textContent?.trim() || '');
  // Inner scroll containers (reasoning <pre>, diff tables, output) keep their own
  // scroll. Save each by a stable key; record whether it was pinned to the bottom
  // so a streaming log stays at the bottom instead of jumping to a stale offset.
  const scrolls = {};
  container.querySelectorAll('[data-scroll-key]').forEach(el => {
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 30;
    scrolls[el.dataset.scrollKey] = { top: el.scrollTop, atBottom };
  });
  return { openDetails, scrollTop: container.scrollTop, scrolls };
}

function restoreUIState(state) {
  if (!state) return;
  const container = document.getElementById('main-content');
  container.scrollTop = state.scrollTop;
  container.querySelectorAll('details').forEach(el => {
    const key = el.dataset.key || el.querySelector('summary')?.textContent?.trim() || '';
    if (state.openDetails.includes(key)) el.open = true;
  });
  const scrolls = state.scrolls || {};
  container.querySelectorAll('[data-scroll-key]').forEach(el => {
    const s = scrolls[el.dataset.scrollKey];
    if (!s) return;
    el.scrollTop = s.atBottom ? el.scrollHeight : s.top;
  });
}

async function loadJob() {
  const uiState = saveUIState();
  try {
    const r = await fetch('/api/job-full/' + encodeURIComponent(JOB_ID));
    if (!r.ok) {
      const d = await r.json().catch(() => ({}));
      document.getElementById('main-content').innerHTML =
        `<div id="error-msg">Job not found: ${escHtml(d.error || r.status)}</div>`;
      return;
    }
    const data = await r.json();
    const diff = await loadDiff(data.job._state);
    renderPage(data.job, data.activity, data.chain, diff);
    restoreUIState(uiState);
  } catch(e) {
    document.getElementById('main-content').innerHTML =
      `<div id="error-msg">Failed to load job: ${escHtml(String(e))}</div>`;
  }
}

let _refreshTimer = null;
function scheduleRefresh(state) {
  if (_refreshTimer) { clearTimeout(_refreshTimer); _refreshTimer = null; }
  if (state === 'running') {
    _refreshTimer = setTimeout(() => loadJob(), 5000);
  }
}
window.addEventListener('beforeunload', () => {
  if (_refreshTimer) clearTimeout(_refreshTimer);
});

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
        elif self.path == "/api/jobs":
            self._api_jobs()
        elif self.path == "/api/repos":
            self._send_json(get_repos(DEFAULT_REPO))
        elif self.path == "/api/ollama-models":
            self._send_json(get_ollama_models() if LOCAL_MODE else [])
        elif self.path == "/api/models":
            self._send_json(fetch_models())
        elif self.path == "/api/worker-stream":
            self._api_worker_stream()
        elif self.path == "/api/worker-status":
            self._send_json({"running": _worker_running})
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
        else:
            self.send_error(404)

    # ── GET handlers ──

    def _serve_dashboard(self) -> None:
        local_badge = (
            f'<span style="font-size:11px;background:#2a1f00;color:#d29922;'
            f'border:1px solid #5a3e1b;border-radius:10px;padding:2px 8px;margin-left:4px">'
            f'🏠 {LOCAL_MODEL}</span>'
            if LOCAL_MODE else ""
        )
        cloud_model = os.environ.get("AGENTIC_MODEL", "auto")
        model_field = (
            '<select id="model"><option value="auto">loading…</option></select>'
            if LOCAL_MODE else
            f'<select id="model" data-current="{cloud_model}"><option value="auto">loading…</option></select>'
        )
        html = (HTML_TEMPLATE
                .replace("__DEFAULT_REPO_HTML__", DEFAULT_REPO)
                .replace("__DEFAULT_REPO_JS__",   json.dumps(DEFAULT_REPO))
                .replace("__LOCAL_BADGE__",        local_badge)
                .replace("__IS_LOCAL__",           "true" if LOCAL_MODE else "false")
                .replace("__MODEL_FIELD__",        model_field))
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
            agentic_bin = AGENTIC_HOME / "bin" / "agentic"
            proc = None
            try:
                proc = subprocess.Popen(
                    [str(agentic_bin), "worker-once"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    start_new_session=True,
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
            repo       = str(body.get("repo", "")).strip() or DEFAULT_REPO
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


# ── Entry point ────────────────────────────────────────────────────────────────

DEFAULT_REPO   = os.getcwd()
PID_FILE       = AGENTIC_HOME / "serve.pid"
LOCAL_MODE     = os.environ.get("AGENTIC_LOCAL", "") == "1"
LOCAL_MODEL    = os.environ.get("AGENTIC_LOCAL_MODEL", "qwen2.5-coder:32b")

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

    server = _Server(("127.0.0.1", port), Handler)
    print(f"agentic dashboard → http://localhost:{port}")
    print("agentic serve stop  — to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
