#!/usr/bin/env python3
"""
Queue operations, job management, and data retrieval for the agentic dashboard.
Imported by serve.py; can also be used independently for testing.
"""

import json
import os
import pathlib
import random
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Literal, NotRequired, TypedDict, cast

# Lang profile — optional import so job_queue can be used standalone
try:
    sys.path.insert(0, str(pathlib.Path(__file__).parent))
    from lang_profile import detect_profile as _detect_profile, load_profile as _load_profile
except Exception:
    def _detect_profile(repo: str) -> str: return "typescript"  # type: ignore[misc]
    def _load_profile(name: str) -> dict[str, Any]: return {}    # type: ignore[misc]

# Risk classifier shared with the worker's live Bash gate — optional so the
# queue still works if the module is absent.
try:
    from diff_guard import command_risk as _command_risk, path_risk as _path_risk
except Exception:
    def _command_risk(command: str) -> str | None: return None    # type: ignore[misc]
    def _path_risk(file_path: str, sandbox_root: str | None = None) -> str | None: return None  # type: ignore[misc]

# ── Domain types ───────────────────────────────────────────────────────────────

class StateHistoryEntry(TypedDict):
    state: str
    at: str
    manual: NotRequired[bool]
    risk_acknowledged: NotRequired[bool]  # set when a flagged job is merged after review

class SessionData(TypedDict, total=False):
    """Parsed Claude session artifacts — only present after get_job_detail()."""
    tasks: Any
    outputs: dict[str, str]
    usage: dict[str, Any]
    validation_issues: str | None
    validation_warnings: str | None
    review: str | None

class ReviewEntry(TypedDict):
    sha: str
    job_id: str
    at: str

class Job(TypedDict):
    id: str
    name: str
    request: str
    target_repo: str
    model_hint: str
    priority: int
    base_branch: str
    parent_request_id: str | None
    submitted_at: str
    submitted_by: str
    state_history: list[StateHistoryEntry]
    summary: str | None
    job_type: NotRequired[str]               # "review" for review jobs
    reviews: NotRequired[list[ReviewEntry]]  # SHA entries appended by each review job
    profile: NotRequired[str]               # language profile name, e.g. "typescript"
    profile_display: NotRequired[str]       # human label, e.g. "TypeScript / React"
    _state: NotRequired[str]                 # injected by find_job, not on disk
    session: NotRequired[SessionData | None] # injected by get_job_detail

class ToolCall(TypedDict):
    name: str
    input: dict[str, Any]   # shape varies per tool
    output: str
    success: bool
    risk_class: NotRequired[str | None]  # network|destructive|sensitive_read|oob_write, set post-hoc

class AgentActivityUnavailable(TypedDict):
    available: Literal[False]

class AgentActivityError(TypedDict):
    available: Literal[True]
    error: str

class AgentActivityData(TypedDict):
    available: Literal[True]
    files_modified: list[str]
    files_read: list[str]
    tool_calls: list[ToolCall]
    assistant_text: str
    build_result: str | None
    lint_result: str | None
    build_error_files: list[str]
    input_tokens: int
    output_tokens: int
    total_tokens: int
    risk_flags: list[dict[str, str]]  # [{risk_class, tool, detail}] — suspicious actions for review
    is_flagged: bool                  # true if any tool call looked suspicious ("something's fishy")

AgentActivity = AgentActivityUnavailable | AgentActivityError | AgentActivityData

class AcceptChainResult(TypedDict):
    accepted: list[str]
    staging_branch: str
    target: str

class ChainResult(TypedDict):
    ok: Literal[True]
    parent: Job | None
    children: list[Job]

class FullJobResult(TypedDict):
    ok: Literal[True]
    job: Job
    activity: AgentActivity
    chain: ChainResult

# ── Paths ──────────────────────────────────────────────────────────────────────

AGENTIC_HOME  = pathlib.Path(os.environ.get("AGENTIC_HOME", pathlib.Path.home() / ".agentic"))
QUEUE_DIR     = AGENTIC_HOME / "queue"
WORKTREES_DIR = AGENTIC_HOME / "worktrees"
DIFFS_DIR     = AGENTIC_HOME / "diffs"
LOGS_DIR      = AGENTIC_HOME / "logs"
STATES         = ("pending", "running", "done", "merged", "failed", "abandoned", "cancelled")
_MAX_DIFF_BYTES = 512 * 1024

# ── Queue helpers ──────────────────────────────────────────────────────────────

def queue_init() -> None:
    for state in STATES:
        (QUEUE_DIR / state).mkdir(parents=True, exist_ok=True)
    WORKTREES_DIR.mkdir(parents=True, exist_ok=True)
    DIFFS_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)


_ADJECTIVES = [
    "amber", "bold", "bright", "calm", "cedar", "clear", "clever", "crisp",
    "dawn", "deft", "eager", "early", "fast", "fine", "fleet", "fresh",
    "gentle", "glad", "golden", "grand", "green", "happy", "hardy", "helpful",
    "jade", "jolly", "keen", "kind", "lively", "lucky", "lunar", "merry",
    "mighty", "nimble", "noble", "oak", "open", "patient", "pine", "polite",
    "proud", "quick", "quiet", "rapid", "ready", "regal", "river", "robust",
    "sage", "sharp", "silver", "simple", "sky", "sleek", "smart", "smooth",
    "solar", "steady", "stellar", "stone", "sturdy", "sunny", "super", "swift",
    "tidy", "true", "vivid", "warm", "wise", "witty", "worthy", "zesty",
]

_NOUNS = [
    "antler", "apex", "aurora", "bay", "beacon", "birch", "breeze", "brook",
    "canyon", "cedar", "cliff", "cloud", "comet", "coral", "creek", "dawn",
    "delta", "dune", "ember", "falcon", "fern", "field", "fjord", "flame",
    "forest", "glade", "glen", "harbor", "haven", "hawk", "helm", "hill",
    "horizon", "island", "jasper", "kettle", "lake", "lantern", "lark",
    "ledge", "maple", "meadow", "mesa", "moon", "mountain", "oak", "ocean",
    "otter", "peak", "pebble", "pine", "pond", "prism", "quartz", "raven",
    "reef", "ridge", "river", "robin", "rock", "sage", "shore", "sierra",
    "sky", "slate", "spruce", "star", "stone", "stream", "summit", "surf",
    "tide", "timber", "trail", "vale", "valley", "vapor", "wave", "willow",
    "wind", "wolf", "woods",
]


def generate_name(existing: set[str]) -> str:
    """Return a unique adjective-noun name not already in existing."""
    attempts = 0
    while attempts < 200:
        name = f"{random.choice(_ADJECTIVES)}-{random.choice(_NOUNS)}"
        if name not in existing:
            return name
        attempts += 1
    # Extremely unlikely: fall back to name with numeric suffix
    return f"{random.choice(_ADJECTIVES)}-{random.choice(_NOUNS)}-{random.randint(2, 99)}"


def _existing_names() -> set[str]:
    names: set[str] = set()
    for state in STATES:
        d = QUEUE_DIR / state
        if not d.is_dir():
            continue
        for f in d.glob("*.json"):
            try:
                data = json.loads(f.read_text())
                n = data.get("name")
                if n:
                    names.add(n)
            except (json.JSONDecodeError, OSError):
                pass
    return names


def new_job_id() -> str:
    suffix = format(random.randint(0, 0xFFFF), "04x")
    return "j_" + time.strftime("%Y%m%d_%H%M%S") + "_" + suffix


def submit_job(request: str, repo: str, priority: int, model_hint: str, after: str | None = None) -> tuple[str, str]:
    result = subprocess.run(
        ["git", "-C", repo, "rev-parse", "--git-dir"],
        capture_output=True,
    )
    if result.returncode != 0:
        raise ValueError(f"Not a git repository: {repo}")

    job_id       = new_job_id()
    name         = generate_name(_existing_names())
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
        "id": job_id, "name": name, "request": request, "target_repo": repo,
        "model_hint": model_hint, "priority": priority,
        "base_branch": base_branch,
        "parent_request_id": after or None,
        "profile": (_pname := _detect_profile(repo)),
        "profile_display": _load_profile(_pname).get("display", _pname),
        "submitted_at": now_iso, "submitted_by": submitted_by,
        "state_history": [{"state": "pending", "at": now_iso}],
        "summary": None,
    }
    filename = f"{priority}_{ts}_{job_id}.json"
    (QUEUE_DIR / "pending" / filename).write_text(json.dumps(job, indent=2))
    return job_id, name


def submit_review_job(parent_job_id: str, comments: list[dict[str, Any]]) -> tuple[str, str]:
    """Submit a review job that commits onto the parent job's branch."""
    parent, _ = find_job(parent_job_id, states=["done"])

    lines = []
    for c in comments:
        file    = str(c.get("file", "")).strip()
        start   = int(c.get("startLine", 0))
        end     = int(c.get("endLine", start))
        comment = str(c.get("comment", "")).strip()
        loc     = f"{file}:{start}:1" if start == end else f"{file}:{start}-{end}:1"
        lines.append(f"{loc}: review: {comment}")

    request = (
        "[Code Review] Address the following review comments on the previous changes.\n"
        "Make surgical edits — focus only on the specific lines mentioned. "
        "Do not redo the whole task.\n\n"
        + "\n".join(lines)
    )

    job_id       = new_job_id()
    name         = generate_name(_existing_names())
    now_iso      = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    submitted_by = f"{os.uname().nodename}:{os.getpid()}"
    ts           = time.strftime("%Y%m%d_%H%M%S")

    job = {
        "id": job_id, "name": name, "request": request,
        "target_repo": parent["target_repo"],
        "model_hint": parent.get("model_hint", "auto"),
        "priority": 0,
        "base_branch": parent.get("base_branch", "HEAD"),
        "parent_request_id": parent_job_id,
        "job_type": "review",
        "submitted_at": now_iso, "submitted_by": submitted_by,
        "state_history": [{"state": "pending", "at": now_iso}],
        "summary": None,
    }
    filename = f"0_{ts}_{job_id}.json"
    (QUEUE_DIR / "pending" / filename).write_text(json.dumps(job, indent=2))
    return job_id, name


def _branch_job_id(job_id: str) -> str:
    """Walk parent_request_id chain up to the root non-review job.

    All review jobs (and reviews of reviews) commit onto the same branch —
    agentic/<root-job-id>. This returns that root job ID.
    """
    seen: set[str] = set()
    current = job_id
    while current and current not in seen:
        seen.add(current)
        try:
            job, _ = find_job(current)
        except ValueError:
            break
        if job.get("job_type") != "review":
            return current
        parent = job.get("parent_request_id")
        if not parent:
            return current
        current = parent
    return job_id  # fallback


def find_job(job_id: str, states: list[str] | None = None) -> tuple[Job, pathlib.Path]:
    """Return (Job, file_path) for a job, searching the given states."""
    for state in (states or STATES):
        d = QUEUE_DIR / state
        if not d.is_dir():
            continue
        for f in d.glob("*.json"):
            try:
                data = json.loads(f.read_text())
                if data.get("id") == job_id:
                    data["_state"] = state
                    return cast(Job, data), f
            except (json.JSONDecodeError, OSError):
                pass
    raise ValueError(f"Job not found: {job_id}")


def cancel_job(job_id: str) -> None:
    data, f = find_job(job_id, states=["pending"])
    data.pop("_state", None)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    data.setdefault("state_history", []).append({"state": "cancelled", "at": now})
    data["summary"] = data.get("summary") or "cancelled via dashboard"
    (QUEUE_DIR / "cancelled" / f.name).write_text(json.dumps(data, indent=2))
    f.unlink()


def accept_job(job_id: str, acknowledge_risk: bool = False) -> str:
    """Merge agentic/<id> into base_branch in target_repo, then remove worktree.

    If the job's activity contains flagged actions (network/exfil/sensitive read/
    out-of-tree write — a possible prompt-injection hijack), refuse to merge until
    the reviewer explicitly acknowledges, so a tainted diff can't be rubber-stamped.
    """
    job, f = find_job(job_id, states=["done"])
    target      = job["target_repo"]
    wt          = WORKTREES_DIR / job_id

    # Anomaly gate — block silent acceptance of a flagged (possibly hijacked) job.
    if not acknowledge_risk:
        try:
            activity = get_agent_activity(job_id)
        except Exception:
            activity = {}  # never let the gate's own failure block a clean merge
        flags = activity.get("risk_flags") if isinstance(activity, dict) else None
        if flags:
            classes = sorted({fl["risk_class"] for fl in flags})
            raise RuntimeError(
                f"This job took {len(flags)} notable action(s) ({', '.join(classes)}) that may "
                f"indicate a prompt-injection hijack. Review the flagged actions, then accept "
                f"again with acknowledgement to merge."
            )

    # All review jobs (including reviews-of-reviews) commit onto the root non-review job's branch
    is_review   = job.get("job_type") == "review"
    branch_root = _branch_job_id(job_id) if is_review else job_id
    branch      = f"agentic/{branch_root}"
    base_branch = job.get("base_branch") or "HEAD"

    if base_branch != "HEAD":
        current = subprocess.run(
            ["git", "-C", target, "symbolic-ref", "--short", "HEAD"],
            capture_output=True, text=True,
        ).stdout.strip()

        if current != base_branch:
            # Switching branches — fail loudly if the working tree is dirty
            dirty = subprocess.run(
                ["git", "-C", target, "status", "--porcelain"],
                capture_output=True, text=True,
            ).stdout.strip()
            if dirty:
                raise RuntimeError(
                    f"Cannot switch to '{base_branch}': you have uncommitted changes. "
                    f"Stash or commit them first, then accept again. "
                    f"Alternatively, use Accept Chain to merge to a staging branch."
                )
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

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    job.pop("_state", None)
    merged_entry: dict[str, Any] = {"state": "merged", "at": now}
    if acknowledge_risk:
        merged_entry["risk_acknowledged"] = True  # audit trail for a flagged merge
    job.setdefault("state_history", []).append(merged_entry)
    (QUEUE_DIR / "merged" / f.name).write_text(json.dumps(job, indent=2))
    f.unlink()

    if is_review:
        # Mark all review ancestors (and root job) merged — they share the same branch
        current_id: str | None = job.get("parent_request_id")
        while current_id:
            try:
                anc, anc_f = find_job(current_id, states=["done"])
                anc.pop("_state", None)
                anc.setdefault("state_history", []).append({"state": "merged", "at": now})
                (QUEUE_DIR / "merged" / anc_f.name).write_text(json.dumps(anc, indent=2))
                anc_f.unlink()
                current_id = anc.get("parent_request_id") if anc.get("job_type") == "review" else None
            except Exception:
                break
    else:
        # Accepting a regular job — cascade-merge all done review descendants (any depth)
        pending = [job_id]
        while pending:
            pid = pending.pop(0)
            for rjob in read_jobs():
                if (rjob.get("parent_request_id") == pid
                        and rjob.get("job_type") == "review"
                        and rjob.get("_state") == "done"):
                    try:
                        rj2, rf = find_job(rjob["id"], states=["done"])
                        rj2.pop("_state", None)
                        rj2.setdefault("state_history", []).append({"state": "merged", "at": now})
                        (QUEUE_DIR / "merged" / rf.name).write_text(json.dumps(rj2, indent=2))
                        rf.unlink()
                        pending.append(rjob["id"])
                    except Exception:
                        pass

    return f"Merged {branch} into {base_branch}"


def accept_chain(job_id: str) -> AcceptChainResult:
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
             if j.get("parent_request_id") == current and j.get("_state") == "done"),
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
        try:
            jjob, jf = find_job(jid, states=["done"])
        except Exception:
            accepted.append(jid)
            continue

        # Review jobs commit onto the parent's branch — their commits are already
        # included when the parent's branch was merged above. Skip the git merge.
        is_review = jjob.get("job_type") == "review"

        if not is_review:
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

        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        jjob.pop("_state", None)
        jjob.setdefault("state_history", []).append({"state": "merged", "at": now})
        (QUEUE_DIR / "merged" / jf.name).write_text(json.dumps(jjob, indent=2))
        jf.unlink()
        accepted.append(jid)

    return cast(AcceptChainResult, {"accepted": accepted, "staging_branch": staging, "target": target})


def abandon_job(job_id: str) -> None:
    """Move a stuck running job to abandoned/ so it can be retried or rejected."""
    data, f = find_job(job_id, states=["running"])
    data.pop("_state", None)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    data.setdefault("state_history", []).append({"state": "abandoned", "at": now})
    data["summary"] = "abandoned via dashboard (worker did not complete)"
    (QUEUE_DIR / "abandoned" / f.name).write_text(json.dumps(data, indent=2))
    f.unlink()


def set_chain(job_id: str, parent_id: str | None) -> None:
    """Set or clear a job's parent_request_id (chain position)."""
    if parent_id == job_id:
        raise ValueError("A job cannot be its own parent")
    data, f = find_job(job_id)
    data.pop("_state", None)
    data["parent_request_id"] = parent_id or None
    f.write_text(json.dumps(data, indent=2))


def set_job_status(job_id: str, new_status: str) -> None:
    """Manually move a job to any state, appending a manual transition to state_history."""
    if new_status not in STATES:
        raise ValueError(f"Invalid status: {new_status}")
    data, f = find_job(job_id)
    if data.get("_state") == new_status:
        return
    data.pop("_state", None)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    data.setdefault("state_history", []).append({"state": new_status, "at": now, "manual": True})
    if new_status == "pending":
        data["summary"] = None  # clear summary on retry
    (QUEUE_DIR / new_status / f.name).write_text(json.dumps(data, indent=2))
    f.unlink()


def review_job(job_id: str) -> str:
    """
    Apply the agent's changes to the working tree as unstaged modifications.

    Uses `git diff base...agent | git apply` — no MERGE_HEAD state, no staging.
    Files appear as ordinary working-tree changes in the IDE. The user edits,
    then commits (or discards with `git checkout -- .`) however they like.
    """
    job, _ = find_job(job_id, states=["done"])
    target      = job["target_repo"]
    base_branch = job.get("base_branch") or "HEAD"
    branch_root = _branch_job_id(job_id) if job.get("job_type") == "review" else job_id
    branch      = f"agentic/{branch_root}"

    # Three-dot diff: changes the agent made since branching, regardless of
    # whether base_branch has moved forward since the job was submitted.
    diff_result = subprocess.run(
        ["git", "-C", target, "diff", f"{base_branch}...{branch}"],
        capture_output=True, text=True,
    )
    if not diff_result.stdout.strip():
        return "No changes to apply — the agent made no commits."

    # Apply to working tree only (not index). --3way falls back to conflict
    # markers if a hunk doesn't apply cleanly, rather than hard-failing.
    apply_result = subprocess.run(
        ["git", "-C", target, "apply", "--3way"],
        input=diff_result.stdout,
        capture_output=True, text=True,
    )
    if apply_result.returncode != 0:
        err = (apply_result.stderr or apply_result.stdout).strip()
        raise RuntimeError(f"Could not apply changes: {err}")

    return (
        f"Changes applied to {target} — review modified files in your IDE, "
        f"then commit. Discard with: git checkout -- ."
    )


def reject_job(job_id: str) -> None:
    """Remove worktree and delete branch without merging."""
    job, _ = find_job(job_id, states=["done", "failed"])
    target  = job["target_repo"]
    wt      = WORKTREES_DIR / job_id

    if wt.exists():
        subprocess.run(
            ["git", "-C", target, "worktree", "remove", str(wt), "--force"],
            capture_output=True,
        )
    # Review jobs commit onto the parent's branch — don't delete it
    if job.get("job_type") != "review":
        subprocess.run(
            ["git", "-C", target, "branch", "-D", f"agentic/{job_id}"],
            capture_output=True,
        )


def delete_job(job_id: str) -> None:
    """Permanently remove a job: JSON file, worktree, branch, diff cache, and log."""
    job, f = find_job(job_id, states=[s for s in STATES if s != "running"])
    target = job["target_repo"]
    wt     = WORKTREES_DIR / job_id

    if wt.exists():
        subprocess.run(
            ["git", "-C", target, "worktree", "remove", str(wt), "--force"],
            capture_output=True,
        )
    # Review jobs commit onto the parent's branch — don't delete it
    if job.get("job_type") != "review":
        subprocess.run(["git", "-C", target, "branch", "-D", f"agentic/{job_id}"], capture_output=True)

    for cache in (DIFFS_DIR / f"{job_id}.diff", LOGS_DIR / f"{job_id}.jsonl"):
        if cache.exists():
            cache.unlink()

    f.unlink()


def get_diff(job_id: str) -> str:
    # Serve from cache — persists after worktree and branch are cleaned up
    cached = DIFFS_DIR / f"{job_id}.diff"
    if cached.exists():
        return cached.read_text(errors="replace")

    diff = ""

    # Resolve job metadata once so both paths can use it
    try:
        job, _ = find_job(job_id)
        is_review   = job.get("job_type") == "review"
        branch_root = _branch_job_id(job_id) if is_review else job_id
        base_branch = job.get("base_branch") or "HEAD"
        target      = job["target_repo"]
    except Exception:
        is_review = False
        branch_root = job_id
        base_branch = "HEAD"
        target = ""

    # Try worktree first (job in progress or not yet accepted)
    wt = WORKTREES_DIR / job_id
    if wt.exists():
        if is_review:
            # Review worktree sits on the root branch; show full diff from base
            r = subprocess.run(
                ["git", "-C", str(wt), "diff", f"{base_branch}...HEAD"],
                capture_output=True, text=True,
            )
            diff = r.stdout
        else:
            for ref in (["HEAD~1"], ["HEAD^"], ["--cached"], []):
                r = subprocess.run(
                    ["git", "-C", str(wt), "diff"] + ref,
                    capture_output=True, text=True,
                )
                if r.stdout.strip():
                    diff = r.stdout
                    break

    # Worktree gone (accepted/rejected) — compute from the branch directly
    if not diff and target:
        try:
            r = subprocess.run(
                ["git", "-C", target, "diff", f"{base_branch}...agentic/{branch_root}"],
                capture_output=True, text=True,
            )
            diff = r.stdout
        except Exception:
            pass

    if diff:
        if len(diff) > _MAX_DIFF_BYTES:
            diff = diff[:_MAX_DIFF_BYTES] + "\n\n[diff truncated at 512KB]\n"
        try:
            cached.write_text(diff)
        except Exception:
            pass

    return diff


def _activity_profile(job_id: str) -> dict[str, Any]:
    """Return the activity sub-section of the job's language profile."""
    try:
        job, _ = find_job(job_id)
        profile = _load_profile(job.get("profile") or "typescript")
        return profile.get("activity", {})
    except Exception:
        return {}


def get_agent_activity(job_id: str) -> AgentActivity:
    """Parse agent JSONL log into rich activity data."""
    # Check persistent cache first — survives worktree cleanup after accept/reject
    cached_log   = LOGS_DIR / f"{job_id}.jsonl"
    worktree_log = WORKTREES_DIR / job_id / ".agent_log.jsonl"

    if cached_log.exists():
        log_file = cached_log
    elif worktree_log.exists():
        log_file = worktree_log
        # Cache it now for future access
        try:
            cached_log.write_bytes(log_file.read_bytes())
        except Exception:
            pass
    else:
        return cast(AgentActivityUnavailable, {"available": False})

    files_read: set[str]     = set()
    files_modified: set[str] = set()
    tool_calls: list[dict[str, Any]]   = []
    assistant_parts: list[str]         = []
    pending: dict[str, dict[str, Any]] = {}
    input_tokens = output_tokens = 0

    try:
        lines = log_file.read_text(errors="replace").splitlines()
    except Exception:
        return cast(AgentActivityError, {"available": True, "error": "Could not read log"})

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
                name     = tu.get("name", "")
                inp      = tu.get("input", {})
                is_error = ev.get("is_error", False)
                content  = ev.get("content", "")
                if isinstance(content, list):
                    content = "\n".join(
                        c.get("text", "") for c in content if isinstance(c, dict)
                    )
                # Build tool outputs a prefix to signal failure even when is_error is False
                if name == "Build" and not is_error:
                    is_error = str(content).startswith("✗")
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

    # Detect key command outcomes from Bash and dedicated Build tool calls
    ap            = _activity_profile(job_id)
    build_cmds    = ap.get("build_commands", ["npm run build", "yarn build", "pnpm build", "vite build", "tsc"])
    lint_cmds     = ap.get("lint_commands",  ["npm run lint", "yarn lint", "eslint", "prettier"])
    err_pattern   = ap.get("error_file_pattern", r"([^\s(]+\.[tj]sx?)\(\d+,\d+\):\s+error")

    build_result = lint_result = None
    for tc in tool_calls:
        if tc["name"] == "Build":
            build_result = "passed" if tc["success"] else "failed"
        elif tc["name"] == "Bash":
            cmd = (tc["input"].get("command") or "").strip()
            if any(x in cmd for x in build_cmds):
                build_result = "passed" if tc["success"] else "failed"
            elif any(x in cmd for x in lint_cmds):
                lint_result = "passed" if tc["success"] else "failed"

    build_error_files: set[str] = set()
    for tc in tool_calls:
        if tc["success"]:
            continue
        if tc["name"] not in ("Build", "Bash"):
            continue
        cmd = (tc["input"].get("command") or "").strip()
        if tc["name"] == "Build" or any(x in cmd for x in build_cmds):
            for m in re.finditer(err_pattern, tc.get("output", "")):
                build_error_files.add(m.group(1))

    # Post-hoc anomaly classification: tag any tool call whose command or path
    # looks like a hijacked agent (network egress, secret read, destructive cmd,
    # out-of-tree write) so the dashboard can flag it for the human who gates the
    # merge. Layers 1-2 in the worker already BLOCK these live; this is the
    # catch-the-rest backstop and the "something's fishy" indicator for the user.
    # The agent reads/writes via ABSOLUTE worktree paths by design, so "absolute
    # path" is not suspicious — only a path that resolves OUTSIDE the worktree, or
    # matches a known-secret name, is. path_risk does the realpath/commonpath
    # containment when we can resolve the worktree; if it's been cleaned up
    # (post-accept), we fall back to the secret-name check only (no false flags).
    wt = WORKTREES_DIR / job_id
    sandbox = str(wt) if wt.exists() else None
    risk_flags: list[dict[str, str]] = []
    for tc in tool_calls:
        rc = None
        if tc["name"] == "Bash":
            rc = _command_risk((tc["input"].get("command") or ""))
            detail = (tc["input"].get("command") or "")[:200]
        elif tc["name"] in ("Write", "Edit", "Read"):
            fp = tc["input"].get("file_path") or ""
            rc = _path_risk(fp, sandbox)
            detail = fp[:200]
        else:
            detail = ""
        tc["risk_class"] = rc
        if rc:
            risk_flags.append({"risk_class": rc, "tool": tc["name"], "detail": detail})

    return cast(AgentActivityData, {
        "available":         True,
        "files_modified":    sorted(files_modified),
        "files_read":        sorted(files_read - files_modified),
        "tool_calls":        tool_calls,
        "assistant_text":    "\n\n".join(assistant_parts),
        "build_result":      build_result,
        "lint_result":       lint_result,
        "build_error_files": sorted(build_error_files),
        "input_tokens":      input_tokens,
        "output_tokens":     output_tokens,
        "total_tokens":      input_tokens + output_tokens,
        "risk_flags":        risk_flags,
        "is_flagged":        bool(risk_flags),
    })


def get_repos(default_repo: str = "") -> list[str]:
    """Return unique target_repos across all jobs, most recently active first.
    default_repo is always included at position 0 if not already present."""
    seen: dict[str, str] = {}  # repo -> latest submitted_at
    for job in read_jobs():
        repo = job.get("target_repo", "")
        at   = job.get("submitted_at", "")
        if repo and (repo not in seen or at > seen[repo]):
            seen[repo] = at
    ordered = sorted(seen, key=lambda r: seen[r], reverse=True)
    if default_repo and default_repo not in seen:
        ordered.insert(0, default_repo)
    return ordered


def read_jobs() -> list[Job]:
    jobs = []
    for state in STATES:
        d = QUEUE_DIR / state
        if not d.is_dir():
            continue
        for f in d.glob("*.json"):
            try:
                data = json.loads(f.read_text())
                data["_state"] = state
                jobs.append(cast(Job, data))
            except Exception:
                pass
    jobs.sort(key=lambda j: j.get("submitted_at", ""), reverse=True)
    return jobs


def get_ollama_models() -> list[str]:
    """Return models available in the local Ollama instance."""
    try:
        result = subprocess.run(
            ["ollama", "list"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return []
        lines = result.stdout.strip().splitlines()
        # First line is a header (NAME  ID  SIZE  MODIFIED)
        return [line.split()[0] for line in lines[1:] if line.strip()]
    except Exception:
        return []


FALLBACK_MODELS = [
    "auto",
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-sonnet-4-6",
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


def get_job_chain(job_id: str) -> ChainResult:
    """Return parent and children for a job's chain visualization."""
    job, _ = find_job(job_id)
    all_jobs = read_jobs()

    parent: Job | None = None
    parent_request_id = job["parent_request_id"]
    if parent_request_id:
        try:
            parent, _ = find_job(parent_request_id)
        except ValueError:
            pass

    children = [j for j in all_jobs if j.get("parent_request_id") == job_id]

    return cast(ChainResult, {
        "ok": True,
        "parent": parent,
        "children": children,
    })


def get_job_detail(job_id: str) -> Job:
    """Return full job data plus a 'session' key with parsed session artifacts."""
    data, _ = find_job(job_id)  # raises ValueError if not found

    session_dir = AGENTIC_HOME / "worktrees" / job_id / ".claude" / "sessions" / f"queued_{job_id}"
    if not session_dir.exists():
        data["session"] = None
        return data

    session: dict[str, Any] = {}

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
    outputs: dict[str, str] = {}
    outputs_dir = session_dir / "outputs"
    if outputs_dir.exists():
        for f in sorted(outputs_dir.glob("task_*.txt")):
            if f.name.endswith("_raw.txt"):
                continue
            stem = f.stem  # e.g. "task_001"
            outputs[stem] = f.read_text()
    session["outputs"] = outputs

    # usage: *_usage.json from session root and outputs/
    usage: dict[str, Any] = {}
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

    data["session"] = cast(SessionData, session)
    return data


def get_job_full(job_id: str) -> FullJobResult:
    """Return job detail + agent activity + chain in a single call."""
    job      = get_job_detail(job_id)
    activity = get_agent_activity(job_id)
    chain    = get_job_chain(job_id)
    return cast(FullJobResult, {"ok": True, "job": job, "activity": activity, "chain": chain})
