"""Deterministic diff & command inspection — the shared substrate for two
defenses that both ask "is this edit/action trustworthy?" without trusting the
model to judge itself:

  1. Correctness: detect "cheat" edits that silence the build instead of fixing
     it (stubbed bodies, suppression tokens on the error line, deleted tests),
     so the repair loop can revert them instead of converging on deletion.
  2. Security (injection): classify a tool call's command/path into a risk_class
     (network/exfil/sensitive_read/oob/destructive) so the dashboard can
     flag a hijacked agent for the human who gates the merge.

Single source of truth for the risk-token sets, imported by both ollama_worker
(live Bash gate) and job_queue (post-hoc activity classifier). Pure stdlib.
"""

from __future__ import annotations

import os
import re
from typing import Optional

# ── Command risk classification ─────────────────────────────────────────────────

# Commands that can move data off the box (the exfil channel).
NET_BINS = frozenset({
    "curl", "wget", "nc", "ncat", "netcat", "socat", "telnet",
    "ssh", "scp", "sftp", "rsync", "ftp", "tftp",
})
# Destructive primitives.
DESTRUCTIVE_BINS = frozenset({"rm", "shred", "dd", "mkfs", "killall", "pkill", "rmdir"})

# Path fragments that indicate a read of something secret/out-of-scope. These are
# advisory flags for the human reviewer, not access control (that's the sandbox
# confinement in ollama_worker); they catch a read that slips through some future
# hole or runs via a tool without confinement.
_SENSITIVE_PATH_RE = re.compile(
    r"(\.ssh/|\.aws/|\.agentic\.conf|\.config/|/etc/(passwd|shadow|sudoers)|"
    r"id_rsa|id_ed25519|\.env(\.|$)|credentials|\.netrc|\.npmrc|\.git-credentials)"
)


def command_risk(command: str) -> Optional[str]:
    """Classify a shell command into a risk_class, or None if it looks benign.

    Token-aware: splits on shell separators so 'a && curl ...' / 'x | nc ...' are
    each inspected, and strips a leading path so '/bin/rm' matches 'rm'.
    Returns one of: 'network', 'destructive', 'sensitive_read', or None.
    """
    if not command:
        return None
    for tok in re.split(r"[\s;|&()<>`]+", command):
        if not tok:
            continue
        base = os.path.basename(tok)
        if base in NET_BINS:
            return "network"
        if base in DESTRUCTIVE_BINS:
            return "destructive"
    if re.search(r"/dev/(tcp|udp)/", command):
        return "network"
    if re.search(r"\bfind\b.*\B-(delete|exec)\b", command):
        return "destructive"
    if re.search(r"\bgit\s+clean\b.*-\w*[fdx]", command):
        return "destructive"
    if _SENSITIVE_PATH_RE.search(command):
        return "sensitive_read"
    return None


def path_risk(file_path: str, sandbox_root: Optional[str] = None) -> Optional[str]:
    """Classify a file path touched by a tool call.

    A known-secret name (.ssh, .agentic.conf, credentials, ...) is the most
    informative signal and wins regardless of location → 'sensitive_read'.
    Otherwise, if a sandbox_root is given and the path resolves OUTSIDE it,
    that's an out-of-bounds access → 'oob'. A path inside the worktree (the
    agent's normal case) is None — not flagged.
    """
    if not file_path:
        return None
    if _SENSITIVE_PATH_RE.search(file_path):
        return "sensitive_read"
    if sandbox_root:
        try:
            target = os.path.realpath(file_path)
            root = os.path.realpath(sandbox_root)
            if os.path.commonpath([root, target]) != root:
                return "oob"
        except (ValueError, OSError):
            return "oob"
    return None


# ── Diff cheat-detection ────────────────────────────────────────────────────────

# Suppression tokens: legitimate in real TypeScript, so their mere presence is
# NOT a cheat. The caller decides — a suppression added ON an error line being
# repaired is a cheat; one added elsewhere is the author's prerogative.
_SUPPRESSION_RE = re.compile(
    r"@ts-ignore|@ts-nocheck|@ts-expect-error|eslint-disable|"
    r"\bas\s+any\b|:\s*any\b|as\s+unknown\s+as\b|#\s*pragma\s+\w*\s*ignore|"
    r"//\s*prettier-ignore"
)
# Unambiguous stubs: a body that does nothing where real logic is expected. These
# are revert-worthy regardless of location during repair.
_STUB_RE = re.compile(
    r"throw\s+new\s+Error\(\s*['\"][^'\"]*not\s+implemented[^'\"]*['\"]|"
    r"\bTODO\b|\bFIXME\b|//\s*stub|/\*\s*stub|"
    r"\breturn\s+null\s*;?\s*$|\breturn\s+undefined\s*;?\s*$|"
    r"\breturn\s*;\s*$|\breturn\s+\{\s*\}\s*;?\s*$|^\s*pass\s*$"
)
# Test tampering: skipping/deleting tests to make a suite green.
_TEST_TAMPER_RE = re.compile(
    r"\.skip\b|\.only\b|xit\(|xdescribe\(|xtest\(|"
    r"pytest\.mark\.skip|@unittest\.skip|@pytest\.mark\.xfail"
)


def _parse_added_lines(diff_text: str) -> list[tuple[str, int, str]]:
    """Yield (file_path, new_line_number, added_line_text) for every '+' line in
    a unified diff. Tracks the post-image line number via @@ hunk headers."""
    added: list[tuple[str, int, str]] = []
    cur_file = ""
    new_lineno = 0
    for raw in diff_text.splitlines():
        if raw.startswith("+++ "):
            # "+++ b/path/to/file" → "path/to/file"
            p = raw[4:].strip()
            cur_file = p[2:] if p.startswith(("a/", "b/")) else p
            continue
        if raw.startswith("@@"):
            m = re.search(r"\+(\d+)", raw)
            new_lineno = int(m.group(1)) if m else 0
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            added.append((cur_file, new_lineno, raw[1:]))
            new_lineno += 1
        elif raw.startswith("-") and not raw.startswith("---"):
            pass  # removed line: does not advance the post-image counter
        elif not raw.startswith("\\"):  # context line ("\ No newline" excluded)
            new_lineno += 1
    return added


def _net_line_counts(diff_text: str) -> tuple[int, int]:
    added = sum(1 for l in diff_text.splitlines()
                if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in diff_text.splitlines()
                  if l.startswith("-") and not l.startswith("---"))
    return added, removed


def scan_diff(diff_text: str, error_locations: Optional[set[tuple[str, int]]] = None) -> dict:
    """Inspect a unified diff for signs a build was made to pass by cheating
    rather than fixing.

    error_locations: set of (file, line) being repaired this round. A suppression
    token added within a few lines of one of these is flagged as 'on_error' — the
    high-confidence "silenced the error instead of solving it" signal. Pass None
    (or empty) outside the repair loop.

    Returns a dict of findings. The caller decides what reverts vs merely warns:
      - on_error_suppressions: list[(file,line,text)]  ← revert-worthy in repair
      - stubs:                 list[(file,line,text)]  ← revert-worthy in repair
      - test_tamper:           list[(file,line,text)]  ← revert-worthy in repair
      - suppressions:          list[(file,line,text)]  ← all suppressions (warn)
      - added/removed/net:     int                      ← scope warning only
    """
    error_locations = error_locations or set()
    added = _parse_added_lines(diff_text)

    suppressions = [(f, n, t) for (f, n, t) in added if _SUPPRESSION_RE.search(t)]
    stubs        = [(f, n, t) for (f, n, t) in added if _STUB_RE.search(t)]
    test_tamper  = [(f, n, t) for (f, n, t) in added if _TEST_TAMPER_RE.search(t)]

    # A suppression "on the error" = added within ±3 lines of a repaired error.
    on_error = []
    for (f, n, t) in suppressions:
        for (ef, eln) in error_locations:
            if (f == ef or f.endswith(ef) or ef.endswith(f)) and abs(n - eln) <= 3:
                on_error.append((f, n, t))
                break

    add_n, rem_n = _net_line_counts(diff_text)
    return {
        "on_error_suppressions": on_error,
        "stubs":                 stubs,
        "test_tamper":           test_tamper,
        "suppressions":          suppressions,
        "added":                 add_n,
        "removed":               rem_n,
        "net":                   add_n - rem_n,
    }


def repair_cheat_reason(findings: dict) -> Optional[str]:
    """Given scan_diff findings, return a short reason if the change is a
    high-confidence cheat that the repair loop should REVERT, else None.

    Deliberately conservative — only the signals that are almost never a
    legitimate fix. Net-deletions and off-error suppressions are intentionally
    NOT reverted (they're dashboard warnings), so legit `as any` / refactors
    don't get the model stuck re-reverting.
    """
    if findings.get("on_error_suppressions"):
        f, n, _ = findings["on_error_suppressions"][0]
        return f"suppressed the build error at {f}:{n} instead of fixing it"
    if findings.get("stubs"):
        f, n, _ = findings["stubs"][0]
        return f"replaced real logic with a stub/TODO at {f}:{n}"
    if findings.get("test_tamper"):
        f, n, _ = findings["test_tamper"][0]
        return f"skipped/disabled a test at {f}:{n}"
    return None
