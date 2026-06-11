#!/usr/bin/env python3
"""
Planning Channels — STORAGE layer (JSON-on-disk under AGENTIC_HOME/channels/).

A *channel* is a per-repo, durable container (keyed by repo path). Inside it live
multiple named *threads*: scoped planning conversations. This module owns the
on-disk tree only — no serve.py wiring, no UI, no agent dispatch, no grounding.

Layout (mirrors queue/, see docs/planning-channels.md §Data model):

    channels/
      <cid>/                       cid = c_<sha1(realpath(repo))[:12]>
        channel.json               header: repo, profile, base_branch, created_at,
                                     index_head_sha
        <tid>.thread.json          per-thread header: id, name, title,
                                     planning_mode, planning_model, timestamps
        <tid>.jsonl                append-only transcript (same line shapes as
                                     logs/<id>.jsonl so activity renderers apply)
        <tid>.citations.jsonl      deduped evidence locker for the thread
        proposals/<pid>.json       derived job set (editable, survives reload)

All writes are atomic (temp file + os.replace); .jsonl files are append-only.
Reuses generate_name / _detect_profile from job_queue. Stdlib only.
"""

import hashlib
import json
import os
import pathlib
import random
import sys
import time
from typing import Any

# Reuse name generator + profile detection from the queue layer. Optional import
# so channels can be exercised standalone (e.g. self-test on a fresh checkout).
sys.path.insert(0, str(pathlib.Path(__file__).parent))
try:
    from job_queue import generate_name as _generate_name
except Exception:  # pragma: no cover - fallback only
    _ADJ = ["calm", "swift", "bright", "keen", "bold"]
    _NOUN = ["river", "peak", "harbor", "ember", "glade"]

    def _generate_name(existing: set[str]) -> str:  # type: ignore[misc]
        for _ in range(200):
            n = f"{random.choice(_ADJ)}-{random.choice(_NOUN)}"
            if n not in existing:
                return n
        return f"{random.choice(_ADJ)}-{random.choice(_NOUN)}-{random.randint(2, 99)}"

try:
    from lang_profile import detect_profile as _detect_profile
except Exception:  # pragma: no cover - fallback only
    def _detect_profile(repo: str) -> str:  # type: ignore[misc]
        return "typescript"

# ── Paths ────────────────────────────────────────────────────────────────────

AGENTIC_HOME = pathlib.Path(os.environ.get("AGENTIC_HOME", pathlib.Path.home() / ".agentic"))
CHANNELS_DIR = AGENTIC_HOME / "channels"


# ── Atomic write helpers (temp + os.replace; copy of the settings.py pattern) ──

def _atomic_write(path: pathlib.Path, text: str) -> None:
    """Write text to path atomically: temp file in the same dir, then os.replace.

    os.replace is atomic on POSIX/Windows when src+dst share a filesystem, so a
    reader never observes a half-written file. The temp name embeds the pid to
    avoid clobbering between concurrent writers in the same directory.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{os.getpid()}.tmp"
    tmp.write_text(text)
    os.replace(tmp, path)


def _atomic_write_json(path: pathlib.Path, data: Any) -> None:
    _atomic_write(path, json.dumps(data, indent=2))


def _append_jsonl(path: pathlib.Path, obj: Any) -> None:
    """Append one JSON object as a line. Append is atomic for small writes on
    POSIX (single write() under O_APPEND); we use text-mode append which is the
    same pattern the worker uses for logs/<id>.jsonl."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(obj, separators=(",", ":"))
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def _read_jsonl(path: pathlib.Path) -> list[Any]:
    """Read a .jsonl file into a list, skipping malformed/blank lines."""
    out: list[Any] = []
    if not path.exists():
        return out
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                out.append(json.loads(raw))
            except json.JSONDecodeError:
                continue
    except OSError:
        pass
    return out


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ── Channel id + git helpers ───────────────────────────────────────────────────

def channel_id_for_repo(repo: str) -> str:
    """Durable per-repo channel id: c_<sha1(realpath(repo))[:12]>.

    realpath canonicalises symlinks so the same repo always maps to the same
    channel regardless of how its path was spelled.
    """
    real = os.path.realpath(repo)
    digest = hashlib.sha1(real.encode("utf-8")).hexdigest()[:12]
    return f"c_{digest}"


def _git_head_sha(repo: str) -> str:
    import subprocess
    try:
        r = subprocess.run(
            ["git", "-C", repo, "rev-parse", "HEAD"],
            capture_output=True, text=True,
        )
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def _git_base_branch(repo: str) -> str:
    """Current branch name (frozen into the channel header, like submit_job)."""
    import subprocess
    try:
        r = subprocess.run(
            ["git", "-C", repo, "symbolic-ref", "--short", "HEAD"],
            capture_output=True, text=True,
        )
        return r.stdout.strip() or "HEAD"
    except Exception:
        return "HEAD"


# ── Init ───────────────────────────────────────────────────────────────────────

def channels_init() -> None:
    """Create the channels/ tree. Like queue_init(): just mkdir, idempotent."""
    CHANNELS_DIR.mkdir(parents=True, exist_ok=True)


# ── Channels ───────────────────────────────────────────────────────────────────

def _channel_dir(cid: str) -> pathlib.Path:
    return CHANNELS_DIR / cid


def _channel_header_path(cid: str) -> pathlib.Path:
    return _channel_dir(cid) / "channel.json"


def channel_create(repo: str) -> dict[str, Any]:
    """Create (or return existing) the channel for a repo.

    cid is derived from the repo path, so this is idempotent per repo: calling it
    twice returns the same channel, never a duplicate. Captures profile, current
    base_branch, and the git HEAD the (future) symbol index will be stamped to.
    """
    channels_init()
    cid = channel_id_for_repo(repo)
    header_path = _channel_header_path(cid)
    if header_path.exists():
        return channel_load(cid)

    real = os.path.realpath(repo)
    header = {
        "id": cid,
        "repo": real,
        "profile": _detect_profile(real),
        "base_branch": _git_base_branch(real),
        "created_at": _now_iso(),
        "index_head_sha": _git_head_sha(real),
    }
    _channel_dir(cid).mkdir(parents=True, exist_ok=True)
    (_channel_dir(cid) / "proposals").mkdir(parents=True, exist_ok=True)
    _atomic_write_json(header_path, header)
    return header


def channel_load(cid: str) -> dict[str, Any]:
    """Load a channel header by id. Raises ValueError if it does not exist."""
    path = _channel_header_path(cid)
    if not path.exists():
        raise ValueError(f"Channel not found: {cid}")
    return json.loads(path.read_text())


def channel_exists(cid: str) -> bool:
    return _channel_header_path(cid).exists()


def channel_update(cid: str, **fields: Any) -> dict[str, Any]:
    """Patch fields on a channel header (e.g. index_head_sha after a reindex)."""
    header = channel_load(cid)
    header.update(fields)
    _atomic_write_json(_channel_header_path(cid), header)
    return header


def channels_list() -> list[dict[str, Any]]:
    """List all channel headers (one per repo)."""
    out: list[dict[str, Any]] = []
    if not CHANNELS_DIR.is_dir():
        return out
    for d in sorted(CHANNELS_DIR.iterdir()):
        if not d.is_dir():
            continue
        hp = d / "channel.json"
        if not hp.exists():
            continue
        try:
            out.append(json.loads(hp.read_text()))
        except (json.JSONDecodeError, OSError):
            continue
    return out


# ── Threads ────────────────────────────────────────────────────────────────────

def _thread_header_path(cid: str, tid: str) -> pathlib.Path:
    return _channel_dir(cid) / f"{tid}.thread.json"


def _thread_transcript_path(cid: str, tid: str) -> pathlib.Path:
    return _channel_dir(cid) / f"{tid}.jsonl"


def _thread_citations_path(cid: str, tid: str) -> pathlib.Path:
    return _channel_dir(cid) / f"{tid}.citations.jsonl"


def _new_thread_id() -> str:
    suffix = format(random.randint(0, 0xFFFF), "04x")
    return "t_" + time.strftime("%Y%m%d_%H%M%S") + "_" + suffix


def _existing_thread_names(cid: str) -> set[str]:
    names: set[str] = set()
    for t in threads_list(cid):
        n = t.get("name")
        if n:
            names.add(n)
    return names


def thread_create(
    cid: str,
    planning_mode: str = "local",
    planning_model: str = "",
    title: str = "",
) -> dict[str, Any]:
    """Create a thread inside a channel. Raises if the channel does not exist."""
    if not channel_exists(cid):
        raise ValueError(f"Channel not found: {cid}")
    tid = _new_thread_id()
    name = _generate_name(_existing_thread_names(cid))
    now = _now_iso()
    header = {
        "id": tid,
        "name": name,
        "title": title,
        "planning_mode": planning_mode,
        "planning_model": planning_model,
        "created_at": now,
        "updated_at": now,
    }
    _atomic_write_json(_thread_header_path(cid, tid), header)
    return header


def thread_load(cid: str, tid: str) -> dict[str, Any]:
    path = _thread_header_path(cid, tid)
    if not path.exists():
        raise ValueError(f"Thread not found: {cid}/{tid}")
    return json.loads(path.read_text())


def thread_exists(cid: str, tid: str) -> bool:
    return _thread_header_path(cid, tid).exists()


def thread_update(cid: str, tid: str, **fields: Any) -> dict[str, Any]:
    """Patch fields on a thread header (e.g. set-model, title). Bumps updated_at."""
    header = thread_load(cid, tid)
    header.update(fields)
    header["updated_at"] = _now_iso()
    _atomic_write_json(_thread_header_path(cid, tid), header)
    return header


def threads_list(cid: str) -> list[dict[str, Any]]:
    """List thread headers in a channel, newest-created last."""
    out: list[dict[str, Any]] = []
    d = _channel_dir(cid)
    if not d.is_dir():
        return out
    for f in d.glob("*.thread.json"):
        try:
            out.append(json.loads(f.read_text()))
        except (json.JSONDecodeError, OSError):
            continue
    out.sort(key=lambda t: t.get("created_at", ""))
    return out


def thread_delete(cid: str, tid: str) -> None:
    """Delete a thread: header, transcript, citations, and its proposals."""
    if not thread_exists(cid, tid):
        raise ValueError(f"Thread not found: {cid}/{tid}")
    for p in (
        _thread_header_path(cid, tid),
        _thread_transcript_path(cid, tid),
        _thread_citations_path(cid, tid),
    ):
        try:
            p.unlink()
        except FileNotFoundError:
            pass
    # Drop proposals owned by this thread.
    for prop in proposals_list(cid, tid):
        try:
            _proposal_path(cid, prop["proposal_id"]).unlink()
        except (FileNotFoundError, KeyError):
            pass


# ── Transcript (append-only) ───────────────────────────────────────────────────

def transcript_append(cid: str, tid: str, entry: dict[str, Any]) -> dict[str, Any]:
    """Append one transcript line. Stamps `at` if missing. Bumps thread updated_at.

    Line shapes mirror logs/<id>.jsonl (see docs/planning-channels.md):
        {"role":"user","text":...,"at":...}
        {"role":"assistant","text":...,"grounding":"index"|"agent","turns":N,
         "citations":[...],"tokens":{in,out},"at":...}
        {"role":"tool","name":"Read","input":{...},"at":...}
        {"role":"draft","proposal_id":...,"at":...}
    """
    if not thread_exists(cid, tid):
        raise ValueError(f"Thread not found: {cid}/{tid}")
    if "at" not in entry:
        entry = {**entry, "at": _now_iso()}
    _append_jsonl(_thread_transcript_path(cid, tid), entry)
    # Touch updated_at without rewriting the whole header race-ily — read-modify.
    try:
        thread_update(cid, tid)
    except ValueError:
        pass
    return entry


def transcript_read(cid: str, tid: str) -> list[dict[str, Any]]:
    return _read_jsonl(_thread_transcript_path(cid, tid))


# ── Citations (deduped append-only locker) ─────────────────────────────────────

def _citation_key(c: dict[str, Any]) -> tuple[Any, ...]:
    """Dedupe identity for a citation: file + range (sha disambiguates content)."""
    return (
        str(c.get("file", "")),
        c.get("start"),
        c.get("end"),
        c.get("sha"),
    )


def citation_append(cid: str, tid: str, citations: list[dict[str, Any]]) -> int:
    """Append citations to the thread's locker, deduped against what's there.

    Returns the count actually written (new ones). A citation is
    {file, start, end, sha, why} per the data model.
    """
    if not thread_exists(cid, tid):
        raise ValueError(f"Thread not found: {cid}/{tid}")
    path = _thread_citations_path(cid, tid)
    existing = {_citation_key(c) for c in _read_jsonl(path)}
    written = 0
    for c in citations:
        key = _citation_key(c)
        if key in existing:
            continue
        existing.add(key)
        if "at" not in c:
            c = {**c, "at": _now_iso()}
        _append_jsonl(path, c)
        written += 1
    return written


def citations_read(cid: str, tid: str) -> list[dict[str, Any]]:
    return _read_jsonl(_thread_citations_path(cid, tid))


# ── Proposals (derived job sets — atomic CRUD) ─────────────────────────────────

def _proposals_dir(cid: str) -> pathlib.Path:
    return _channel_dir(cid) / "proposals"


def _proposal_path(cid: str, pid: str) -> pathlib.Path:
    return _proposals_dir(cid) / f"{pid}.json"


def _new_proposal_id() -> str:
    suffix = format(random.randint(0, 0xFFFF), "04x")
    return "p_" + time.strftime("%Y%m%d_%H%M%S") + "_" + suffix


def proposal_create(
    cid: str,
    tid: str,
    jobs: list[dict[str, Any]],
    summary: str = "",
) -> dict[str, Any]:
    """Create a draft proposal owned by a thread.

    A proposal mirrors the Job shape loosely:
        {proposal_id, thread_id, summary, status:"draft"|"submitted",
         jobs:[{seq,title,request,depends_on,anchors}], submitted_job_ids:[]}
    Each job's seq is assigned positionally so depends_on indices are stable.
    """
    if not thread_exists(cid, tid):
        raise ValueError(f"Thread not found: {cid}/{tid}")
    pid = _new_proposal_id()
    norm_jobs: list[dict[str, Any]] = []
    for i, j in enumerate(jobs):
        norm_jobs.append({
            "seq": i,
            "title": j.get("title", ""),
            "request": j.get("request", ""),
            "depends_on": j.get("depends_on"),
            "anchors": j.get("anchors", []),
        })
    proposal = {
        "proposal_id": pid,
        "thread_id": tid,
        "summary": summary,
        "status": "draft",
        "jobs": norm_jobs,
        "submitted_job_ids": [],
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    }
    _atomic_write_json(_proposal_path(cid, pid), proposal)
    return proposal


def proposal_load(cid: str, pid: str) -> dict[str, Any]:
    path = _proposal_path(cid, pid)
    if not path.exists():
        raise ValueError(f"Proposal not found: {cid}/{pid}")
    return json.loads(path.read_text())


def proposal_save(cid: str, pid: str, proposal: dict[str, Any]) -> dict[str, Any]:
    """Overwrite a proposal with an edited copy (atomic). Bumps updated_at and
    preserves the proposal_id. Used by the editable proposal drawer."""
    if not _proposal_path(cid, pid).exists():
        raise ValueError(f"Proposal not found: {cid}/{pid}")
    proposal = {**proposal, "proposal_id": pid, "updated_at": _now_iso()}
    _atomic_write_json(_proposal_path(cid, pid), proposal)
    return proposal


def proposal_update(cid: str, pid: str, **fields: Any) -> dict[str, Any]:
    """Patch specific fields on a proposal (e.g. status, submitted_job_ids)."""
    proposal = proposal_load(cid, pid)
    proposal.update(fields)
    proposal["updated_at"] = _now_iso()
    _atomic_write_json(_proposal_path(cid, pid), proposal)
    return proposal


def proposals_list(cid: str, tid: str | None = None) -> list[dict[str, Any]]:
    """List proposals in a channel, optionally filtered to one thread."""
    out: list[dict[str, Any]] = []
    d = _proposals_dir(cid)
    if not d.is_dir():
        return out
    for f in d.glob("*.json"):
        try:
            p = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if tid is not None and p.get("thread_id") != tid:
            continue
        out.append(p)
    out.sort(key=lambda p: p.get("created_at", ""))
    return out


def proposal_delete(cid: str, pid: str) -> None:
    try:
        _proposal_path(cid, pid).unlink()
    except FileNotFoundError:
        raise ValueError(f"Proposal not found: {cid}/{pid}")


# ── Self-test ──────────────────────────────────────────────────────────────────

def _self_test() -> None:
    """Exercise the full storage surface against a temp AGENTIC_HOME + temp repo."""
    import subprocess
    import tempfile

    tmp_home = tempfile.mkdtemp(prefix="chan_home_")
    tmp_repo = tempfile.mkdtemp(prefix="chan_repo_")

    # Rebind module paths to the temp home for an isolated test.
    global AGENTIC_HOME, CHANNELS_DIR
    AGENTIC_HOME = pathlib.Path(tmp_home)
    CHANNELS_DIR = AGENTIC_HOME / "channels"

    # Make tmp_repo a real git repo so base_branch / head_sha resolve.
    subprocess.run(["git", "-C", tmp_repo, "init", "-q"], check=True)
    subprocess.run(["git", "-C", tmp_repo, "config", "user.email", "t@t.t"], check=True)
    subprocess.run(["git", "-C", tmp_repo, "config", "user.name", "t"], check=True)
    (pathlib.Path(tmp_repo) / "f.txt").write_text("hello\n")
    subprocess.run(["git", "-C", tmp_repo, "add", "-A"], check=True)
    subprocess.run(["git", "-C", tmp_repo, "commit", "-qm", "init"], check=True)

    channels_init()
    assert CHANNELS_DIR.is_dir(), "channels_init did not create the tree"

    # Channel: create is idempotent per repo.
    ch = channel_create(tmp_repo)
    cid = ch["id"]
    assert cid.startswith("c_") and len(cid) == 14, f"bad cid: {cid}"
    assert ch["repo"] == os.path.realpath(tmp_repo)
    assert ch["base_branch"] in ("main", "master", "HEAD"), ch["base_branch"]
    assert ch["index_head_sha"], "expected a HEAD sha"
    ch2 = channel_create(tmp_repo)
    assert ch2["id"] == cid, "channel_create is not idempotent"
    assert len(channels_list()) == 1, "duplicate channel created"
    assert channel_load(cid)["id"] == cid

    # Thread create + load + list.
    th = thread_create(cid, planning_mode="local", planning_model="qwen2.5-coder:32b",
                       title="where is the parser?")
    tid = th["id"]
    assert tid.startswith("t_")
    assert th["name"] and "-" in th["name"], f"bad name: {th['name']}"
    assert thread_load(cid, tid)["title"] == "where is the parser?"
    assert len(threads_list(cid)) == 1

    # set-model style update.
    th2 = thread_update(cid, tid, planning_mode="cloud", planning_model="claude-opus-4-8")
    assert th2["planning_mode"] == "cloud"
    assert th2["updated_at"] >= th2["created_at"]

    # Transcript append + read (user then grounded assistant turn).
    transcript_append(cid, tid, {"role": "user", "text": "where is the parser?"})
    transcript_append(cid, tid, {
        "role": "assistant", "text": "It's in lib/stream_parser.py.",
        "grounding": "agent", "turns": 2,
        "citations": [{"file": "lib/stream_parser.py", "start": 1, "end": 40,
                       "sha": "abc", "why": "defines the parser"}],
        "tokens": {"in": 1200, "out": 80},
    })
    tx = transcript_read(cid, tid)
    assert len(tx) == 2, f"expected 2 transcript lines, got {len(tx)}"
    assert tx[0]["role"] == "user" and "at" in tx[0]
    assert tx[1]["grounding"] == "agent" and tx[1]["turns"] == 2

    # Citations: deduped append.
    n1 = citation_append(cid, tid, [
        {"file": "lib/stream_parser.py", "start": 1, "end": 40, "sha": "abc", "why": "x"},
        {"file": "lib/job_queue.py", "start": 205, "end": 239, "sha": "def", "why": "y"},
    ])
    assert n1 == 2, f"expected 2 new citations, got {n1}"
    n2 = citation_append(cid, tid, [
        {"file": "lib/stream_parser.py", "start": 1, "end": 40, "sha": "abc", "why": "dup"},
    ])
    assert n2 == 0, f"expected dedupe to write 0, got {n2}"
    assert len(citations_read(cid, tid)) == 2

    # Proposal: write, read, edit-save, status update.
    prop = proposal_create(cid, tid, jobs=[
        {"title": "Add null guard", "request": "Fix null handling. Read lib/x.py:1-20.",
         "depends_on": None, "anchors": [{"file": "lib/x.py", "start": 1, "end": 20}]},
        {"title": "Wire it up", "request": "Call the guard from y.", "depends_on": 0,
         "anchors": []},
    ], summary="2-job chain")
    pid = prop["proposal_id"]
    assert pid.startswith("p_")
    assert prop["jobs"][0]["seq"] == 0 and prop["jobs"][1]["seq"] == 1
    assert prop["jobs"][1]["depends_on"] == 0
    assert prop["status"] == "draft"

    loaded = proposal_load(cid, pid)
    assert loaded["proposal_id"] == pid
    loaded["jobs"][0]["title"] = "Add null guard (edited)"
    proposal_save(cid, pid, loaded)
    assert proposal_load(cid, pid)["jobs"][0]["title"] == "Add null guard (edited)"

    proposal_update(cid, pid, status="submitted", submitted_job_ids=["j_1", "j_2"])
    after = proposal_load(cid, pid)
    assert after["status"] == "submitted"
    assert after["submitted_job_ids"] == ["j_1", "j_2"]
    assert len(proposals_list(cid, tid)) == 1
    assert len(proposals_list(cid)) == 1

    # Thread delete also removes its proposals + jsonl.
    thread_delete(cid, tid)
    assert not thread_exists(cid, tid)
    assert not _thread_transcript_path(cid, tid).exists()
    assert not _thread_citations_path(cid, tid).exists()
    assert len(proposals_list(cid)) == 0, "proposals not cleaned on thread delete"
    assert len(threads_list(cid)) == 0

    print("OK")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--self-test":
        _self_test()
    else:
        _self_test()
