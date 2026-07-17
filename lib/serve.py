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
    set_chain, set_backend, set_job_status, reject_job, delete_job, review_job, submit_review_job,
    get_diff, get_agent_activity, fetch_models, get_ollama_models,
    get_job_chain, get_job_detail, get_job_full, get_repos,
)
import settings as _settings
import channels as _channels
import planner as _planner
from slots import POOL

# ── Per-job worker buffers (DICT-KEYED, shape-identical to the _ask_* pattern) ─
# The old singleton _worker_* buffer assumed one worker at a time. With the
# concurrency rework we run N workers (across the local + cloud pools), so every
# buffer is now keyed by job_id, exactly like the proven planning-channel
# _ask_* buffers below. Each job_id owns a log list, a done flag, a return code,
# its own Condition, a meta dict (backend/proc/pid/name), and a GC timestamp.
# Reconnecting SSE clients replay the buffer; closing the browser never stops
# the worker (the dispatcher thread owns the subprocess lifetime).
_w_lock = threading.Lock()
_w_log:  dict[str, list[dict]] = {}   # job_id -> ordered event dicts (line/progress)
_w_done: dict[str, bool] = {}         # job_id -> stream complete
_w_rc:   dict[str, int | None] = {}   # job_id -> worker exit code
_w_cond: dict[str, threading.Condition] = {}  # job_id -> waiter condition
_w_meta: dict[str, dict] = {}         # job_id -> {backend, proc, pid, name, ...}
_w_gc:   dict[str, float] = {}        # job_id -> monotonic stamp when finished (for GC)
_w_final: dict[str, "int | None"] = {}  # job_id -> terminal rc, SURVIVES buffer GC (bounded)


def _w_buf(job_id: str) -> threading.Condition:
    """Get (creating if needed) the per-job condition guarding its buffer.
    Mirrors _ask_buf — first touch lazily allocates every parallel slot for the
    job_id so _w_emit/_w_finish/tailers all share one Condition."""
    with _w_lock:
        cond = _w_cond.get(job_id)
        if cond is None:
            cond = threading.Condition()
            _w_cond[job_id] = cond
            _w_log.setdefault(job_id, [])
            _w_done.setdefault(job_id, False)
            _w_rc.setdefault(job_id, None)
            _w_meta.setdefault(job_id, {})
        return cond


def _w_emit(job_id: str, ev: dict) -> None:
    """Append one event to a job's buffer and wake all SSE tailers (mirrors
    _ask_emit)."""
    cond = _w_buf(job_id)
    with cond:
        _w_log[job_id].append(ev)
        cond.notify_all()


def _w_finish(job_id: str, rc: int | None) -> None:
    """Mark a job's worker stream complete, record its return code, stamp it for
    GC, and wake tailers a final time (mirrors _ask_finish)."""
    cond = _w_buf(job_id)
    with cond:
        _w_rc[job_id]   = rc
        _w_done[job_id] = True
        _w_gc[job_id]   = time.monotonic()
        cond.notify_all()


# ── Dispatcher state (slot-driven, both backends) ──────────────────────────────
# The dispatcher fills free slots from the queue for each backend. _disp_lock
# guards all of (_drain, _pending_n, _active) AND the claim+spawn serialization;
# it is NEVER held while a Condition wait happens and is NEVER nested under the
# POOL lock or a _w_cond. _disp_cond/_disp_seq/_disp_log are the reconnect INDEX
# for /api/dispatch-stream — a ring of dispatch-level events (start/end/drained/
# truncated) that the frontend uses to discover which per-job streams exist.
_DISP_RING_CAP = 2000
_disp_lock = threading.Lock()
_disp_cond = threading.Condition()         # standalone — notified on dispatch events only
_disp_seq  = 0                             # monotonic event sequence (next id to assign)
_disp_base = 0                             # seq of the oldest event still in the ring
_disp_log: list[dict] = []                 # ring of {seq, ...event} dicts, cap _DISP_RING_CAP

_BACKENDS = ("local", "cloud")
_drain:     dict[str, bool] = {"local": False, "cloud": False}   # Run-All latch per backend
_pending_n: dict[str, int]  = {"local": 0, "cloud": 0}           # one-shot Run-Worker requests
_active:    dict[str, dict] = {}            # job_id -> {backend, proc, started} for live workers
_leak_seen: dict[str, int]  = {"local": 0, "cloud": 0}  # watchdog: persistent slot-leak tracking
# Planning agents (chat + derive) also consume POOL slots but are NOT in _active
# (they're not worker subprocesses). Track how many planning slots are held per
# backend so the slot-leak reaper compares POOL.used against workers+planning,
# not workers alone — otherwise it wrongly reclaims a live planning slot.
_plan_held_lock = threading.Lock()
_plan_held: dict[str, int] = {"local": 0, "cloud": 0}


def _plan_acquire(backend: str) -> bool:
    """Take a POOL slot for a planning agent and record it as planning-held."""
    got = POOL.try_acquire(backend)
    if got:
        with _plan_held_lock:
            _plan_held[backend] = _plan_held.get(backend, 0) + 1
    return got


def _plan_release(backend: str) -> None:
    """Release a planning-held POOL slot (mirrors _plan_acquire)."""
    with _plan_held_lock:
        if _plan_held.get(backend, 0) > 0:
            _plan_held[backend] -= 1
    POOL.release(backend)


def _emit_disp(ev: dict) -> None:
    """Append a dispatch-level event to the reconnect ring and wake dispatch-stream
    tailers. Assigns a monotonic seq, caps the ring at _DISP_RING_CAP (advancing
    _disp_base so reconnecting clients can detect truncation). Notifies ONLY here
    — never per worker log line — so dispatch-stream stays a thin index."""
    global _disp_seq, _disp_base
    with _disp_cond:
        ev = {**ev, "seq": _disp_seq}
        _disp_seq += 1
        _disp_log.append(ev)
        if len(_disp_log) > _DISP_RING_CAP:
            drop = len(_disp_log) - _DISP_RING_CAP
            del _disp_log[:drop]
            _disp_base += drop
        _disp_cond.notify_all()

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
# Live planning subprocess per thread, so a running chat/derive can be cancelled.
_ask_proc: "dict[str, subprocess.Popen]" = {}


def _register_ask_proc(tid: str, proc: "subprocess.Popen") -> None:
    with _ask_lock:
        _ask_proc[tid] = proc


def _cancel_ask_proc(tid: str) -> bool:
    """Kill the in-flight planning subprocess for a thread (whole process group,
    so the agent and any child stop). Returns True if something was killed."""
    with _ask_lock:
        proc = _ask_proc.get(tid)
    if proc is None or proc.poll() is not None:
        return False
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception:
        try:
            proc.terminate()
        except Exception:
            return False
    return True


def _planning_backend(channel_dict: dict, thread_dict: dict) -> str:
    """Resolve a planning thread's backend ('local'|'cloud') the same way
    planner.ask does: thread.planning_mode → channel default → 'local'. Cloud
    mode = cloud pool; anything else = local pool. Used so planning agents
    (chat + derive) consume the SAME slot pool as worker jobs — a local chat
    competes with a local worker for Ollama, so it must count against it."""
    mode = thread_dict.get("planning_mode") or channel_dict.get("default_mode") or "local"
    return "cloud" if mode == "cloud" else "local"


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


# ── Dispatcher (slot-driven worker fan-out) ────────────────────────────────────
# The dispatcher replaces the old "one worker, recursion in the browser" model.
# It fills free POOL slots for BOTH backends from the queue, serializing
# acquire+claim+spawn under _disp_lock, and self-rearms: each worker's _run,
# on finish, releases its slot OUTSIDE _disp_lock then calls _pump() to refill.
#
# Backend is DERIVED from the existing job field model_hint (no new field, no
# migration): "local"->local pool, "remote"->cloud pool, "auto"->the server's
# default mode (settings.json "mode"). Chain siblings never run concurrently:
# _claim excludes any chain root already active or in running/.
#
# LOCK ORDER (strict, never violated):
#   _disp_lock  is leaf-ish: taken alone for state; POOL.acquire/release and
#               _w_*/_emit_disp Conditions are taken OUTSIDE it (acquire is done
#               inside _pump before claim, but POOL has its own lock and is never
#               nested under a _w_cond). A slot RELEASE always happens OUTSIDE
#               _disp_lock. We never wait on a Condition while holding _disp_lock.

_AGENTIC_BIN = Path(os.environ.get("AGENTIC_APP", str(AGENTIC_HOME))) / "bin" / "agentic"


def _backend_of(job: dict) -> str:
    """Resolve a job's execution backend from its model_hint. 'local'->local,
    'remote'->cloud, 'auto'/anything-else->LOCAL. Returns 'local' or 'cloud'.
    This is the ONLY place the model_hint->pool mapping lives. There is no global
    'mode' anymore (backend is per-job): an unset/auto job defaults to local, and
    queue.sh's _queue_job_backend MUST use the identical rule so the claim filter
    and the dispatcher never disagree."""
    hint = str(job.get("model_hint", "auto") or "auto").strip().lower()
    if hint == "remote":
        return "cloud"
    return "local"   # 'local', 'auto', blank, or anything unexpected → local


def _chain_root(job_id: str) -> str:
    """Walk parent_request_id up to the first non-review job — the chain root,
    matching job_queue._branch_job_id semantics. Used to serialize chain
    siblings: two jobs sharing a root must never run at once."""
    seen: set[str] = set()
    current = job_id
    while current and current not in seen:
        seen.add(current)
        try:
            job, _ = find_job(current)
        except Exception:
            break
        if job.get("job_type") != "review":
            return current
        parent = job.get("parent_request_id")
        if not parent:
            return current
        current = parent
    return job_id


def _excluded_chain_roots() -> list[str]:
    """Chain roots that must NOT be claimed right now: the root of every job in
    _active, plus the root of every job currently in running/. Prevents two
    siblings of one chain from running concurrently (correctness).

    CALLED ONLY from _claim, which already holds _disp_lock — so we read _active
    WITHOUT re-acquiring _disp_lock (it is non-reentrant; re-locking would
    deadlock). The running/ scan needs no lock (filesystem snapshot)."""
    roots: set[str] = set()
    # Active (in-flight) workers — caller holds _disp_lock.
    for jid in list(_active.keys()):
        roots.add(_chain_root(jid))
    # Anything already sitting in running/ (claimed by us or another process).
    try:
        running_dir = AGENTIC_HOME / "queue" / "running"
        if running_dir.is_dir():
            for f in running_dir.glob("*.json"):
                try:
                    data = json.loads(f.read_text())
                except Exception:
                    continue
                jid = data.get("id")
                if jid:
                    roots.add(_chain_root(jid))
    except Exception:
        pass
    return sorted(r for r in roots if r)


def _claim(backend: str, excluded: "list[str] | None" = None) -> dict | None:
    """Atomically claim one pending job for `backend` via queue.sh's queue_claim,
    passing --backend and --exclude-chain-roots so the shell skips non-matching
    backends and chain siblings. Returns the claimed Job dict (now in running/),
    or None when nothing is claimable. Never raises into the dispatcher.

    `excluded` is the in-flight chain-root set, snapshotted by the caller UNDER
    _disp_lock and passed in — this function runs the claim subprocess and must
    NOT itself touch _active (it is called OUTSIDE _disp_lock to avoid blocking
    the whole dispatcher on a multi-second shell claim)."""
    if excluded is None:
        excluded = _excluded_chain_roots()
    # Phase 0 exposes queue.sh's queue_claim as `agentic claim`, which prints the
    # claimed job id (now moved to running/) on success and nothing on an empty
    # claim. --backend filters by model_hint→pool; --exclude-chain-roots skips
    # chain siblings already running.
    cmd = [str(_AGENTIC_BIN), "claim", "--backend", backend]
    if excluded:
        cmd += ["--exclude-chain-roots", ",".join(excluded)]
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30, env=os.environ.copy(),
        )
    except Exception:
        return None
    lines = (out.stdout or "").strip().splitlines()
    claimed_id = lines[-1].strip() if lines else ""
    if not claimed_id or out.returncode != 0:
        return None
    # queue_claim prints the claimed job id; load it from running/.
    try:
        job, _ = find_job(claimed_id, states=["running"])
        return dict(job)
    except Exception:
        return None


def _spawn(job: dict, backend: str) -> "subprocess.Popen[str] | None":
    """Launch `agentic worker-once --id <job_id> --backend <backend>` as a
    detached process group, mirroring the per-spawn env that the old _run_worker
    built. The dispatcher already claimed the job, so worker-once SKIPS its own
    claim and reads running/<id>. --backend sets AGENTIC_LOCAL per-process."""
    job_id = job["id"]
    _cfg = _settings.load()
    _env = {
        **os.environ,
        # worker-once --backend will also set AGENTIC_LOCAL; we set it here too so
        # any early code in bin/agentic sees a consistent value per spawn.
        "AGENTIC_LOCAL":       "1" if backend == "local" else "",
        "AGENTIC_LOCAL_MODEL": _cfg.get("local_model", "qwen-coder:latest"),
        "AGENTIC_CHAIN_GATE":  "1" if _cfg.get("pause_chain_for_review") else "",
    }
    if backend == "cloud":
        _key = _settings.get_secret("ANTHROPIC_API_KEY")
        if _key:
            _env["ANTHROPIC_API_KEY"] = _key
        _env["AGENTIC_MODEL"] = _cfg.get("cloud_model", "auto")
    else:
        # Ensure no stale cloud model leaks into a local spawn.
        _env.pop("AGENTIC_MODEL", None)
    try:
        return subprocess.Popen(
            [str(_AGENTIC_BIN), "worker-once", "--id", job_id, "--backend", backend],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
            env=_env,
        )
    except Exception:
        return None


def _ingest_line(job_id: str, line: str) -> None:
    """Route one raw worker stdout line into the job's buffer. A PROGRESS
    sentinel (from stream_parser) becomes a {progress} event for the header
    counter; everything else is a {line} event."""
    if line.startswith("\x01PROGRESS "):
        try:
            prog = json.loads(line[len("\x01PROGRESS "):])
        except Exception:
            prog = None
        if prog is not None:
            _w_emit(job_id, {"progress": prog})
            return
    _w_emit(job_id, {"line": line})


def _run(job_id: str, backend: str, proc: "subprocess.Popen[str]") -> None:
    """Worker lifetime thread: stream the subprocess's stdout into the per-job
    buffer, then on finish: pop _active, RELEASE the slot OUTSIDE _disp_lock,
    finalize the buffer, emit the dispatch-level `end` event, and re-arm the
    pump to fill the freed slot. The process outlives any SSE connection."""
    rc: int | None = 1
    try:
        if proc.stdout is not None:
            for raw in iter(proc.stdout.readline, ""):
                _ingest_line(job_id, raw.rstrip("\n"))
        proc.wait()
        rc = proc.returncode
    except Exception:
        rc = proc.returncode if proc.returncode is not None else 1
    finally:
        # Single-owner teardown: popping _active[job_id] is the ownership token.
        # Whoever pops it (this thread OR the watchdog's dead-proc reaper, never
        # both) is the sole party that releases the slot, finalizes the buffer,
        # and emits `end`. This closes the TOCTOU where the reaper could reap a
        # job in the stdout-EOF window and double-release / double-emit.
        _finalize_worker(job_id, backend, rc)


def _finalize_worker(job_id: str, backend: str, rc: "int | None") -> None:
    """Idempotent, exactly-once teardown for one worker. Returns immediately if
    another party already owned (popped) this job. The pop under _disp_lock is
    the atomic claim of ownership."""
    with _disp_lock:
        owned = _active.pop(job_id, None) is not None
    if not owned:
        return  # someone else (reaper or a prior call) already finalized this job
    POOL.release(backend)                 # release OUTSIDE _disp_lock (POOL self-locks)
    _w_finish(job_id, rc if rc is not None else 1)
    _emit_disp({"type": "end", "job_id": job_id, "backend": backend, "rc": rc})
    _pump()                               # a freed slot may admit a queued job


def _pump() -> None:
    """Idempotent dispatch step: for each backend, while there is a free slot AND
    either a Run-All drain is latched or a one-shot Run-Worker request is pending,
    try to acquire a slot, claim a matching job, and spawn it. Serializes
    acquire+claim+spawn under _disp_lock. Safe to call from anywhere (control
    endpoints, submit-while-draining, a worker finishing) — it just fills what it
    can and returns.

    Drain latch handling (lost-wakeup guard): on an EMPTY claim we only clear the
    one-shot pending counter, never the _drain latch while a worker for this
    backend is still active — a chain child can become eligible the moment a
    parent finishes, and clearing drain here would strand it. We emit {drained}
    only when nothing is claimable for a backend AND no worker for that backend
    is still active."""
    for backend in _BACKENDS:
        while True:
            # Phase A1 — under _disp_lock: gate on demand, take a slot, and
            # snapshot the in-flight chain roots. We hold the lock only for these
            # fast in-memory ops, NOT across the claim subprocess.
            with _disp_lock:
                if not (_drain[backend] or _pending_n[backend] > 0):
                    break  # no demand for this backend
                if not POOL.try_acquire(backend):
                    break  # pool full — a future release() re-pumps us
                excluded = _excluded_chain_roots()  # reads _active under the lock

            # Phase A2 — OUTSIDE the lock: the claim shells out to queue.sh and can
            # take a while; holding _disp_lock across it would freeze status/stop/
            # other pumps. The atomic `mv` inside queue_claim still guarantees no
            # double-claim even with two pumps racing here.
            job = _claim(backend, excluded)

            # Phase A3 — back under _disp_lock: commit the result.
            with _disp_lock:
                if job is None:
                    # Nothing claimable now: return the slot, consume one one-shot
                    # request, and clear the drain latch ONLY if no worker for this
                    # backend is still running (else a chain child may yet appear).
                    POOL.release(backend)
                    if _pending_n[backend] > 0:
                        _pending_n[backend] -= 1
                    if _drain[backend] and not any(
                        m["backend"] == backend for m in _active.values()
                    ):
                        _drain[backend] = False
                    break  # nothing more to do this round
                proc = _spawn(job, backend)
                job_id = job["id"]
                if _pending_n[backend] > 0:
                    _pending_n[backend] -= 1
                if proc is None:
                    POOL.release(backend)
                    outcome = "failed"
                else:
                    _active[job_id] = {"backend": backend, "proc": proc,
                                       "started": time.monotonic()}
                    outcome = "spawned"
            # ── outside _disp_lock ──
            if outcome == "failed":
                # Spawn failed: synthesize a finished stream so the UI never hangs.
                _w_emit(job_id, {"line": "worker spawn failed"})
                _w_finish(job_id, 1)
                _emit_disp({"type": "end", "job_id": job_id,
                            "backend": backend, "rc": 1})
                continue  # try another job for this backend
            # Spawn OK: seed meta, emit the dispatch start event, run the worker.
            cond = _w_buf(job_id)
            with cond:
                _w_meta[job_id].update({"backend": backend, "proc": proc,
                                        "pid": proc.pid,
                                        "name": job.get("name", job_id)})
            _emit_disp({"type": "start", "job_id": job_id, "backend": backend,
                        "name": job.get("name", job_id),
                        "request": job.get("request", "")})
            t = threading.Thread(target=_run, args=(job_id, backend, proc),
                                 daemon=True)
            t.start()
            # Loop again to try filling another free slot for this backend.
        # After the while: announce drained when this backend is fully idle.
        # Dedupe consecutive drained events for the same backend so repeated pumps
        # (initial + each worker's refill pump) don't spam the index.
        with _disp_lock:
            idle = (not any(m["backend"] == backend for m in _active.values())
                    and _pending_n[backend] == 0 and not _drain[backend])
        if idle:
            with _disp_cond:
                last = next((e for e in reversed(_disp_log)
                             if e.get("backend") == backend), None)
                already = bool(last and last.get("type") == "drained")
            if not already:
                _emit_disp({"type": "drained", "backend": backend})


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
  <!-- Row 1 — identity + status (no controls): title, model badges, pool counts, freshness -->
  <div class="header-row header-info">
    <div class="dot pulse"></div>
    <h1>agentic</h1>
    __LOCAL_BADGE__
    <span id="pool-chip-slot"></span>
    <span id="age">loading…</span>
  </div>
  <!-- Row 2 — controls: view switch + worker buttons + settings -->
  <div class="header-row header-controls">
    <div id="view-toggle">
      <button id="vt-queue" class="vt-btn active" onclick="setView('queue')">Queue</button>
      <button id="vt-channels" class="vt-btn" onclick="setView('channels')">Channels</button>
    </div>
    <button id="run-btn" onclick="runWorker(false)">▶ Run Worker</button>
    <button id="run-all-btn" onclick="runWorker(true)" style="background:#1f4391;color:#88b4ff;padding:6px 14px;border-radius:6px;border:none;cursor:pointer;font-size:13px;font-weight:500;margin-left:4px;">▶▶ Run All</button>
    <button id="stop-btn" onclick="stopWorker()" style="display:none;background:#6d2120;color:#f47067;padding:6px 14px;border-radius:6px;border:1px solid #6d2120;cursor:pointer;font-size:13px;font-weight:500;margin-left:4px;">■ Stop</button>
    <button id="settings-btn" onclick="openSettings()" title="Settings" style="display:none;background:none;border:1px solid #21262d;color:#8b949e;padding:6px 10px;border-radius:6px;cursor:pointer;font-size:14px;margin-left:auto;">⚙</button>
  </div>
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
      <label class="field"><span class="field-label">Backend</span>
        <select id="backend" title="Which engine runs this job. Local = Ollama; Cloud = Claude. Set per job; you can change it later while the job is still pending.">
          <option value="local">🏠 Local (Ollama)</option>
          <option value="cloud">☁ Cloud (Claude)</option>
        </select>
      </label>
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
    <div style="font-size:11px;color:#6e7681;margin-top:6px">Cmd+Enter / Ctrl+Enter to submit · each job runs on its own <b>backend</b> (Local or Cloud) — change it on a pending job's card any time before it runs.</div>
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
      <button id="ch-send-btn" class="btn btn-green" onclick="askQuestion()">Send</button>
      <button id="ch-stop-btn" class="btn btn-amber" style="display:none" onclick="cancelPlanning()" title="Stop the running answer">■ Stop</button>
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
    <button id="prop-stop-btn" class="btn btn-amber" style="display:none" onclick="cancelPlanning()" title="Stop the running derivation">■ Stop</button>
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
        elif self.path == "/api/dispatch-stream" or self.path.startswith("/api/dispatch-stream?"):
            self._api_dispatch_stream()
        elif self.path.startswith("/api/worker-stream/"):
            job_id = self.path.split("/api/worker-stream/", 1)[-1].split("?", 1)[0].strip("/")
            self._api_worker_stream_job(job_id)
        elif self.path == "/api/worker-status" or self.path.startswith("/api/worker-status?"):
            self._api_worker_status()
        elif self.path == "/api/channels":
            self._api_channels_list()
        elif self.path == "/api/channels/models":
            self._api_channels_models()
        elif self.path.startswith("/api/ask-reconnect"):
            self._api_ask_reconnect()
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
        elif self.path == "/api/run-worker":
            self._api_run_worker()
        elif self.path == "/api/run-all":
            self._api_run_all()
        elif self.path == "/api/pool":
            self._api_pool()
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
        elif self.path == "/api/set-backend":
            self._api_set_backend()
        elif self.path == "/api/settings":
            self._api_save_settings()
        elif self.path == "/api/secrets":
            self._api_save_secret()
        elif self.path == "/api/channel/create":
            self._api_channel_create()
        elif self.path == "/api/ask-cancel":
            self._api_ask_cancel()
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
        # Backend is per-job now, so the header shows BOTH pools' models — a job
        # can run on either. Local = amber 🏠, cloud = blue ☁; each shows the
        # model that backend resolves to from Settings.
        _cm = _settings.load().get("cloud_model", "auto")
        mode_badge = (
            f'<span style="font-size:11px;background:#2a1f00;color:#d29922;'
            f'border:1px solid #5a3e1b;border-radius:10px;padding:2px 8px;margin-left:4px" '
            f'title="Model local jobs run on (Settings → Local model)">'
            f'🏠 {local_model()}</span>'
            f'<span style="font-size:11px;background:#0d2440;color:#58a6ff;'
            f'border:1px solid #1f4391;border-radius:10px;padding:2px 8px;margin-left:4px" '
            f'title="Model cloud jobs run on (Settings → Cloud model)">'
            f'☁ {_cm}</span>'
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

    # ── Dispatch + per-job streaming (T6) ──

    def _api_dispatch_stream(self) -> None:
        """GET /api/dispatch-stream?since=<seq> — the reconnect INDEX.

        Streams only dispatch-level events (start/end/drained/truncated) from the
        _disp_log ring, so the frontend can discover which per-job worker streams
        exist (and open one each). It carries NO worker log lines — those flow on
        /api/worker-stream/<job_id>. On reconnect, `since` replays everything with
        seq >= since; if `since` predates the ring (events were dropped), we send
        one {type:"truncated", base_seq} so the client knows to resync, then
        replay from the ring base. Tails on _disp_cond, which is notified ONLY by
        _emit_disp — never per log line — so this stream is cheap."""
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(self.path).query)
        try:
            since = int((qs.get("since") or ["0"])[0])
        except ValueError:
            since = 0

        send = self._sse_open()
        cursor = since
        while True:
            with _disp_cond:
                base = _disp_base
                # Snapshot events with seq >= cursor from the ring.
                if cursor < base:
                    truncated_to = base
                    pending = list(_disp_log)
                else:
                    truncated_to = None
                    pending = [e for e in _disp_log if e["seq"] >= cursor]
            if truncated_to is not None:
                if not send({"type": "truncated", "base_seq": truncated_to}):
                    return
                cursor = truncated_to
                continue
            for ev in pending:
                if not send(ev):
                    return
                cursor = ev["seq"] + 1
            if not pending:
                with _disp_cond:
                    _disp_cond.wait(timeout=15.0)

    def _api_worker_stream_job(self, job_id: str) -> None:
        """GET /api/worker-stream/<job_id>?cursor=<n> — per-job tail.

        Replays the job's buffered events (line/progress) from `cursor`, then
        tails until the worker finishes (then emits done{rc[,gc]}). The client is
        always an EventSource, so EVERY response — including the terminal/GC-d
        case — must be an SSE frame, never JSON (a JSON body fires the browser's
        onerror, not onmessage, and the carefully-returned state is never read)."""
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(self.path).query)
        try:
            cursor = int((qs.get("cursor") or ["0"])[0])
        except ValueError:
            cursor = 0

        # Buffer already GC-d, or a job we never buffered: there is nothing to
        # tail, but the job DID finish. Emit the recorded terminal rc (preserved
        # in _w_final across GC) as a single SSE `done` frame so the UI renders
        # the correct ✓/✗ instead of hanging or showing a false failure.
        with _w_lock:
            known = job_id in _w_cond
            final_rc = _w_final.get(job_id, None)
            had_final = job_id in _w_final
        if not known:
            send = self._sse_open()
            send({"done": True, "rc": final_rc, "gc": had_final})
            return

        cond = _w_buf(job_id)
        send = self._sse_open()
        while True:
            with cond:
                snapshot = _w_log.get(job_id, [])[cursor:]
                done = _w_done.get(job_id, False)
                rc   = _w_rc.get(job_id)
                gc   = job_id in _w_gc
            for ev in snapshot:
                if not send(ev):
                    return  # browser gone — worker keeps running
            cursor += len(snapshot)
            if done:
                send({"done": True, "rc": rc, "gc": gc})
                return
            if not snapshot:
                with cond:
                    cond.wait(timeout=2.0)

    # ── Dispatch control + status (T7) ──

    def _api_run_worker(self) -> None:
        """POST /api/run-worker {backend?} — one-shot: run ONE more job. With an
        explicit backend, targets that pool; WITHOUT one (the header Run button),
        it kicks BOTH pools so the next claimable job of either backend runs.
        Bumps the per-backend pending counter(s) and pumps."""
        try:
            body = self._read_body()
            backends = self._resolve_backends(body.get("backend"))
            with _disp_lock:
                for b in backends:
                    _pending_n[b] += 1
            _pump()
            self._send_json({"ok": True, "backends": backends})
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, 500)

    def _api_run_all(self) -> None:
        """POST /api/run-all {backend?} — latch the drain so the dispatcher keeps
        claiming+running matching jobs until the queue is empty. With an explicit
        backend, drains that pool; WITHOUT one (the header Run All button), drains
        BOTH — "run everything" regardless of each job's backend."""
        try:
            body = self._read_body()
            backends = self._resolve_backends(body.get("backend"))
            with _disp_lock:
                for b in backends:
                    _drain[b] = True
            _pump()
            self._send_json({"ok": True, "backends": backends})
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, 500)

    def _resolve_backends(self, raw: Any) -> list:
        """An explicit 'local'/'cloud' → just that pool; anything else (absent/
        auto/blank) → BOTH pools. With per-job backends there is no single global
        mode, so an unspecified Run/Run-All means 'run whatever's queued'."""
        b = str(raw or "").strip().lower()
        if b in ("local", "cloud"):
            return [b]
        return list(_BACKENDS)

    def _resolve_backend(self, raw: Any) -> str:
        """Normalize a request 'backend' field to 'local'|'cloud'; absent/auto →
        local (there is no global mode — backend is per-job)."""
        b = str(raw or "").strip().lower()
        return "cloud" if b == "cloud" else "local"

    def _api_stop_worker(self) -> None:
        """POST /api/stop-worker {job_id} | {all:true} — SIGTERM a worker's whole
        process group (targeted by job_id, or every active worker for {all}).
        For {all}, also clear both drain latches and pending counters so the
        dispatcher stops re-spawning. The dying procs' _run finalizers clean up
        slots and buffers."""
        try:
            body = self._read_body()
            stop_all = bool(body.get("all"))
            job_id   = str(body.get("job_id", "")).strip()
            if stop_all:
                with _disp_lock:
                    for b in _BACKENDS:
                        _drain[b] = False
                        _pending_n[b] = 0
                    targets = [(jid, m.get("proc")) for jid, m in _active.items()]
                killed = 0
                for jid, proc in targets:
                    if self._killpg(proc):
                        killed += 1
                self._send_json({"ok": True, "stopped": killed})
                return
            if not job_id:
                self._send_json({"ok": False, "error": "job_id or all required"}, 400)
                return
            with _disp_lock:
                meta = _active.get(job_id)
                proc = meta.get("proc") if meta else None
            if proc is None:
                self._send_json({"ok": False, "error": "no active worker for that job"}, 404)
                return
            self._killpg(proc)
            self._send_json({"ok": True})
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, 500)

    @staticmethod
    def _killpg(proc: "subprocess.Popen[str] | None") -> bool:
        """SIGTERM a worker's process group; True if a signal was delivered (or it
        was already gone). Its _run finalizer releases the slot + buffer."""
        if proc is None:
            return False
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            return True
        except ProcessLookupError:
            return True  # already dead — its finalizer will reap
        except Exception:
            return False

    def _api_worker_status(self) -> None:
        """GET /api/worker-status — rich dispatcher snapshot for the header chips:
        per-backend pool usage (used/max), whether each backend is draining, how
        many pending jobs map to each backend, and the live active worker list."""
        try:
            pools = POOL.snapshot()
            with _disp_lock:
                draining = dict(_drain)
                pending_oneshot = dict(_pending_n)
                active_ids = [(jid, m.get("backend"), m.get("started"))
                              for jid, m in _active.items()]
            # name lives in _w_meta (under _w_lock) so a reconnect rebuilds panes
            # with the friendly adjective-noun label, not the raw job id.
            with _w_lock:
                active = [
                    {"job_id": jid, "backend": b, "started": started,
                     "name": _w_meta.get(jid, {}).get("name", jid)}
                    for jid, b, started in active_ids
                ]
            # Count pending-queue jobs per backend by their derived backend.
            pending_by_backend = {"local": 0, "cloud": 0}
            try:
                pending_dir = AGENTIC_HOME / "queue" / "pending"
                if pending_dir.is_dir():
                    for f in pending_dir.glob("*.json"):
                        try:
                            job = json.loads(f.read_text())
                        except Exception:
                            continue
                        b = _backend_of(job)
                        pending_by_backend[b] = pending_by_backend.get(b, 0) + 1
            except Exception:
                pass
            self._send_json({
                "ok": True,
                "pools": pools,
                "draining": draining,
                "pending_oneshot": pending_oneshot,
                "pending": pending_by_backend,
                "active": active,
                "running": bool(active),  # back-compat flag for old UI
            })
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, 500)

    def _api_pool(self) -> None:
        """POST /api/pool {backend, max} — set a backend's max slots live (and
        persist it to the matching Settings knob so it survives restart). Applies
        immediately via POOL.configure, then pumps in case raising the cap freed
        capacity."""
        try:
            body = self._read_body()
            backend = self._resolve_backend(body.get("backend"))
            try:
                new_max = int(body.get("max"))
            except (TypeError, ValueError):
                self._send_json({"ok": False, "error": "max must be an integer"}, 400)
                return
            if new_max < 1:
                self._send_json({"ok": False, "error": "max must be >= 1"}, 400)
                return
            # Persist to the matching knob, then reconfigure POOL from settings so
            # the other backend keeps its current cap.
            key = "ollama_num_parallel" if backend == "local" else "cloud_max_workers"
            cfg = _settings.save({key: new_max})
            POOL.configure(int(cfg.get("ollama_num_parallel", 2)),
                           int(cfg.get("cloud_max_workers", 4)))
            _pump()
            self._send_json({"ok": True, "pools": POOL.snapshot()})
        except (TypeError, ValueError):
            self._send_json({"ok": False, "error": "max must be an integer"}, 400)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, 500)

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
            # For a chained child this is the cumulative chain diff (base...HEAD)
            # — parent foundation + this job's delta — so reviewing the tip
            # reviews the whole chain, and any line is commentable.
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
            # If a Run-All drain is latched for any backend, a newly-submitted job
            # should be picked up without waiting for the next pump trigger. Read
            # _drain under _disp_lock to honor the "never touch dispatcher state
            # outside the lock" invariant.
            with _disp_lock:
                drain_any = any(_drain.values())
            if drain_any:
                _pump()
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
            # Apply the two concurrency knobs to the live pool so a Settings save
            # changes capacity without a restart (then pump in case a cap rose).
            try:
                POOL.configure(int(resolved.get("ollama_num_parallel", 2)),
                               int(resolved.get("cloud_max_workers", 4)))
                _pump()
            except Exception:
                pass
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

    def _api_set_backend(self) -> None:
        """POST /api/set-backend {id, backend:'local'|'cloud'} — switch a PENDING
        job's execution backend (stored as model_hint). 400 if not pending."""
        try:
            body = self._read_body()
            job_id  = str(body.get("id", "")).strip()
            backend = str(body.get("backend", "")).strip()
            if not job_id:
                self._send_json({"ok": False, "error": "id required"}, 400)
                return
            set_backend(job_id, backend)
            self._send_json({"ok": True, "backend": backend})
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

    def _api_ask_reconnect(self) -> None:
        """GET /api/ask-reconnect?tid — re-attach to a planning run already in
        flight for this thread (e.g. after the user navigated away and back).
        Tails the per-thread buffer WITHOUT starting a new run or re-recording the
        question. If no run is active, returns {ok, running:false} as JSON so the
        client just renders the persisted transcript. The run itself lives in a
        daemon thread and is unaffected by the browser disconnecting."""
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(self.path).query)
        tid = (qs.get("tid") or [""])[0]
        if not tid:
            self._send_json({"ok": False, "error": "tid required"}, 400)
            return
        with _ask_lock:
            running = _ask_running.get(tid, False)
        if not running:
            # Nothing in flight — the answer (if any) is already in the transcript.
            self._send_json({"ok": True, "running": False})
            return
        send = self._sse_open()
        self._stream_thread_buffer(tid, send)

    def _api_ask_cancel(self) -> None:
        """POST /api/ask-cancel {tid} — stop an in-flight planning run (chat or
        derive) for a thread by killing its subprocess. The run's finally then
        releases its slot and emits its error/finish to any tailing stream."""
        try:
            tid = str(self._read_body().get("tid", "")).strip()
            if not tid:
                self._send_json({"ok": False, "error": "tid required"}, 400)
                return
            killed = _cancel_ask_proc(tid)
            # Nudge the stream to wrap up even if the kill was a no-op (already done).
            if killed:
                _ask_emit(tid, {"type": "error", "error": "stopped by user"})
            self._send_json({"ok": True, "stopped": killed})
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, 500)

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

        # Capture prior conversation BEFORE appending the current question, so the
        # current turn does NOT duplicate into the multi-turn history block.
        history_text, dropped = self._build_history_text(cid, tid)

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

        _ask_backend = _planning_backend(channel_dict, thread_dict)

        def _run():
            # A planning agent consumes a slot in the SAME pool as worker jobs so
            # the header chip and ceiling reflect ALL work on a backend (a local
            # chat competes with a local worker for Ollama). try_acquire is
            # non-blocking and advisory here — we always run the chat, but the
            # acquire/release keeps the live count accurate.
            _configure_pool_from_settings()
            _got_slot = _plan_acquire(_ask_backend)
            # Surface that older turns were dropped to fit the history budget.
            if dropped > 0:
                _ask_emit(tid, {"type": "context_trimmed", "dropped": dropped})
            try:
                smap, _ = _planner.cached_symbol_map(cid, channel["repo"], channel.get("profile"))
            except Exception:
                smap = None
            try:
                res = _planner.ask(
                    channel_dict, thread_dict, q,
                    symbol_map=smap, dig_deeper=dig,
                    history=history_text,
                    on_event=lambda ev: _ask_emit(tid, ev),
                    on_start=lambda p: _register_ask_proc(tid, p),
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
                if _got_slot:
                    _plan_release(_ask_backend)
                with _ask_lock:
                    _ask_proc.pop(tid, None)
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
        # Derivation gets its OWN (higher) turn budget: covering a multi-concern
        # plan means opening/anchoring many files, so the single-question cap
        # would run out mid-derivation and drop the later concerns. A per-thread
        # explicit value still wins.
        if not thread_dict.get("planning_max_turns"):
            thread_dict["planning_max_turns"] = _settings.get("derive_max_turns")

        send = self._sse_open()
        _derive_backend = _planning_backend(channel_dict, thread_dict)

        def _run():
            # Deriving runs a planning agent — count it against the same pool as
            # worker jobs + chats so the header chip reflects it (the bug: derive
            # never took a slot, so local stayed 0/2 while an agent was running).
            _configure_pool_from_settings()
            _got_slot = _plan_acquire(_derive_backend)
            try:
                prop = _planner.derive(
                    channel_dict, thread_dict, transcript_text,
                    on_event=lambda ev: _ask_emit(tid, ev),
                    on_start=lambda p: _register_ask_proc(tid, p),
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
                if _got_slot:
                    _plan_release(_derive_backend)
                with _ask_lock:
                    _ask_proc.pop(tid, None)
                _ask_finish(tid)

        threading.Thread(target=_run, daemon=True).start()
        self._stream_thread_buffer(tid, send)

    def _submitted_line(self, e: dict) -> str:
        """One-line summary of a 'submitted' transcript entry: what jobs were
        queued (seq, title, request). Shared by the chat-history and derivation
        context builders so continued conversation / re-derivation knows what
        has already been queued from this thread."""
        jobs = e.get("jobs", []) or []
        if not jobs:
            return ""
        parts = []
        for idx, j in enumerate(jobs):
            seq = j.get("seq")
            num = (seq + 1) if isinstance(seq, int) and not isinstance(seq, bool) else idx + 1
            title = j.get("title") or j.get("name") or j.get("job_id") or "job"
            req = (j.get("request") or "").strip()
            parts.append(f"({num}) {title}" + (f" — {req}" if req else ""))
        return f"Queued {len(jobs)} job(s) as a chain: " + "; ".join(parts)

    def _transcript_text(self, cid: str, tid: str) -> str:
        """Flatten the thread transcript to plain text for the derivation agent.
        Includes already-queued jobs so a second derivation doesn't re-propose
        work that's already in the queue."""
        lines = []
        for e in _channels.transcript_read(cid, tid):
            role = e.get("role", "")
            if role in ("user", "assistant"):
                lines.append(f"{role.upper()}: {e.get('text', '')}")
            elif role == "submitted":
                s = self._submitted_line(e)
                if s:
                    lines.append(f"ALREADY QUEUED: {s}")
        return "\n\n".join(lines)

    def _build_history_text(self, cid: str, tid: str, budget_tokens: int = 6000):
        """Build the PRIOR CONVERSATION block for a planning follow-up.

        Returns (history_text, dropped). Empty ("", 0) when there are no prior
        turns. Includes 'submitted' turns (jobs queued from this thread) so a
        follow-up knows what's already been queued and can build on it. Trims
        oldest turns to fit budget_tokens (estimated as len(block)//4), but never
        drops below the most recent 2 turns."""
        turns = []  # list of (label, text)
        for e in _channels.transcript_read(cid, tid):
            role = e.get("role", "")
            if role in ("user", "assistant"):
                text = e.get("text", "")
                if text:
                    turns.append(("User" if role == "user" else "Assistant", text))
            elif role == "submitted":
                s = self._submitted_line(e)
                if s:
                    turns.append(("Jobs queued", s))
        if not turns:
            return "", 0

        header = "PRIOR CONVERSATION (most recent last):"

        def _render(ts):
            lines = [header]
            for label, text in ts:
                lines.append(f"{label}: {text}")
            return "\n".join(lines)

        dropped = 0
        block = _render(turns)
        # Estimate tokens as len(block)//4; drop oldest turns while over budget,
        # keeping at least the most recent 2 turns (one exchange).
        while len(block) // 4 > budget_tokens and len(turns) > 2:
            turns.pop(0)
            dropped += 1
            block = _render(turns)
        return block, dropped

    def _channel_submit(self, cid: str, tid: str) -> None:
        """POST /api/channel/<cid>/<tid>/submit {proposal_id, included_seqs} —
        walk jobs in seq order and queue them as ONE STRICTLY LINEAR CHAIN: each
        job's parent is the previously-queued job, so job N branches off job N-1's
        result and builds on it. We deliberately do NOT honor the model's
        depends_on as the parent, because a fan-out (several jobs sharing one
        parent) makes each sibling branch off the SAME base independently — their
        changes diverge and collide when the chain is accepted. Linearizing by seq
        still respects every declared dependency (depends_on always points to an
        earlier seq — no forward refs — so seq order is a valid topological order).
        Only job['request'] carries forward (job-isolation rule)."""
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
        # Linear chain: each queued job's parent is the previous one we queued
        # (in seq order), regardless of the model's depends_on graph. This
        # guarantees a single sequential chain — no two siblings share a parent,
        # so no divergent branches and no accept-time merge races.
        prev_job_id: str | None = None
        results = []
        try:
            for j in jobs:
                seq = int(j.get("seq", 0))
                if include_set is not None and seq not in include_set:
                    continue
                request = str(j.get("request", "")).strip()
                if not request:
                    continue
                after = prev_job_id  # chain onto the previously-queued job
                # ONLY the request string crosses into the job — no thread,
                # transcript, channel, or proposal reference is ever written.
                job_id, job_name = submit_job(request, repo, priority, model_hint, after)
                prev_job_id = job_id
                # Keep title + request on the thread-side record so the chat can
                # show what was queued and continued conversation has the context.
                # (This is thread→job context; the job itself still carries ONLY
                # its request string — the isolation rule above is unchanged.)
                results.append({"seq": seq, "job_id": job_id, "name": job_name,
                                "title": j.get("title", ""), "request": request})
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

# Backend is per-job now (a job's model_hint → local/cloud pool); there is no
# global "mode". is_local() is retained only for the few UI affordances that ask
# "is local available?" — always true (local is always a usable backend), and the
# header shows both pools' models regardless.
def is_local() -> bool:
    return True

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

def _configure_pool_from_settings() -> None:
    """Size both pools from the current Settings knobs (call at start + on save)."""
    cfg = _settings.load()
    try:
        POOL.configure(int(cfg.get("ollama_num_parallel", 2)),
                       int(cfg.get("cloud_max_workers", 4)))
    except Exception:
        pass


def _watchdog_loop() -> None:
    """ONE persistent daemon (10s tick) that keeps the dispatcher honest:

      1) Slot-leak reaper — if POOL reports more used slots for a backend than we
         have live worker procs in _active, a worker died without its _run
         finalizer releasing (e.g. an unexpected kill). Release the difference and
         re-pump so the freed capacity is used.
      2) Dead-proc reaper — a proc in _active whose poll() is set but whose _run
         never finished (finalizer crashed): synthesize the finish (drop from
         _active, release its slot, _w_finish, emit end) so nothing hangs.
      3) Buffer GC — drop per-job _w_* buffers 60s after their _w_gc stamp so a
         long-lived server doesn't accumulate finished-job buffers forever."""
    while True:
        try:
            time.sleep(10.0)
        except Exception:
            return
        try:
            now = time.monotonic()
            # ── 2) Dead-proc reaper (do this first so leak counts are accurate). ──
            # Identify procs whose OS process exited but whose _run never reached
            # finalize (a stuck/crashed finalizer). Snapshot candidates under the
            # lock, then hand each to _finalize_worker — the SAME single-owner
            # path _run uses. _finalize_worker's atomic _active.pop is the
            # ownership token, so if _run's finally races us, exactly one of us
            # finalizes (no double-release, no duplicate `end`).
            dead = []
            with _disp_lock:
                for jid, m in list(_active.items()):
                    proc = m.get("proc")
                    if proc is not None and proc.poll() is not None:
                        dead.append((jid, m.get("backend", "local"),
                                     proc.returncode))
            for jid, backend, rc in dead:
                _finalize_worker(jid, backend, rc if rc is not None else 1)

            # ── 1) Slot-leak reaper (only for PERSISTENT leaks). ──
            # A real leak (a thread died before releasing) persists; a transient
            # used>live window — where _finalize_worker has popped _active but not
            # yet called POOL.release — resolves in microseconds. Acting on the
            # transient window would double-release. So we only reclaim a slot
            # whose discrepancy persisted across TWO consecutive 10s ticks for the
            # same backend (tracked in _leak_seen), which a teardown window never
            # survives.
            snap = POOL.snapshot()
            with _disp_lock:
                live = {b: 0 for b in _BACKENDS}
                for m in _active.values():
                    b = m.get("backend")
                    if b in live:
                        live[b] += 1
            # Planning agents (chat/derive) hold slots too but aren't in _active.
            # Count them as live so the reaper doesn't reclaim a slot a live
            # planning agent is using (the bug: a derive's slot got "leaked" back
            # to 0 mid-run after ~20s).
            with _plan_held_lock:
                for b in _BACKENDS:
                    live[b] += _plan_held.get(b, 0)
            leaked = False
            for b in _BACKENDS:
                used = int(snap.get(b, {}).get("used", 0))
                diff = used - live.get(b, 0)
                prev = _leak_seen.get(b, 0)
                if diff > 0:
                    # reclaim only the count that was ALSO over last tick
                    reclaim = min(diff, prev)
                    for _ in range(reclaim):
                        POOL.release(b)
                        leaked = True
                    _leak_seen[b] = diff  # remember for next tick
                else:
                    _leak_seen[b] = 0
            if leaked:
                _pump()

            # ── 3) Buffer GC (60s after finish). ──
            # Drop the big per-job buffers but PRESERVE the terminal rc in
            # _w_final so a late reconnect to a long-finished job still renders
            # the correct ✓/✗ instead of an unknown (rc=None → false failure).
            with _w_lock:
                stale = [jid for jid, t in list(_w_gc.items()) if now - t > 60.0]
                for jid in stale:
                    _w_final[jid] = _w_rc.get(jid, 0)   # remember the outcome
                    _w_log.pop(jid, None)
                    _w_done.pop(jid, None)
                    _w_rc.pop(jid, None)
                    _w_cond.pop(jid, None)
                    _w_meta.pop(jid, None)
                    _w_gc.pop(jid, None)
                # Bound _w_final so it can't grow forever (keep newest ~500).
                if len(_w_final) > 500:
                    for k in list(_w_final)[:len(_w_final) - 500]:
                        _w_final.pop(k, None)
        except Exception:
            # The watchdog must never die — swallow and tick again.
            pass


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

    # Size the slot pools from Settings, then start the single watchdog daemon
    # (slot-leak / dead-proc reaper + buffer GC). Both must be up before serving.
    _configure_pool_from_settings()
    threading.Thread(target=_watchdog_loop, daemon=True).start()

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
