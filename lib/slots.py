"""Shared slot pool — gates concurrent work per backend.

Two pools live here:

  * LOCAL  — gates concurrent *local* work (Ollama jobs AND chat/planning
             channels) at ``ollama_num_parallel`` slots. One Ollama server can
             only run so many generations in parallel; over-subscribing it just
             trashes throughput, so every local consumer must take a slot first.
  * CLOUD  — gates concurrent *cloud* (Claude Code) work at
             ``cloud_max_workers`` slots, bounding how many ``claude`` CLI
             subprocesses we fan out at once.

The module exposes a single process-wide ``POOL`` singleton. Acquisition is
**non-blocking** (``try_acquire`` returns ``False`` immediately when the cap is
hit) so the dispatcher stays in control of scheduling instead of parking
threads. The pool owns its own :class:`threading.Lock` and must NEVER be locked
while holding any other lock (keep it a leaf in the lock order).
"""

from __future__ import annotations

import threading

LOCAL = "local"
CLOUD = "cloud"

DEFAULT_LOCAL_MAX = 2
DEFAULT_CLOUD_MAX = 4


class _Pool:
    """Per-backend non-blocking counting semaphore.

    All mutations are guarded by a single private lock. ``used`` never drops
    below 0 and ``max`` is clamped to be non-negative.
    """

    def __init__(self, local_max: int = DEFAULT_LOCAL_MAX,
                 cloud_max: int = DEFAULT_CLOUD_MAX) -> None:
        self._lock = threading.Lock()
        self._max = {
            LOCAL: max(0, int(local_max)),
            CLOUD: max(0, int(cloud_max)),
        }
        self._used = {LOCAL: 0, CLOUD: 0}

    def try_acquire(self, backend: str) -> bool:
        """Take one slot for ``backend`` if available.

        Returns ``True`` if a slot was acquired, ``False`` if the backend is at
        (or over) its cap. Non-blocking.
        """
        with self._lock:
            if backend not in self._used:
                return False
            if self._used[backend] < self._max[backend]:
                self._used[backend] += 1
                return True
            return False

    def release(self, backend: str) -> None:
        """Return one slot to ``backend``. ``used`` floors at 0."""
        with self._lock:
            if backend not in self._used:
                return
            if self._used[backend] > 0:
                self._used[backend] -= 1

    def configure(self, local_max: int | None = None,
                  cloud_max: int | None = None) -> None:
        """Resize pool caps live. ``None`` leaves a cap unchanged.

        Shrinking below the currently-used count is allowed: in-flight work
        keeps running, but no new slots are handed out until ``used`` drains
        back under the new cap.
        """
        with self._lock:
            if local_max is not None:
                self._max[LOCAL] = max(0, int(local_max))
            if cloud_max is not None:
                self._max[CLOUD] = max(0, int(cloud_max))

    def snapshot(self) -> dict[str, dict[str, int]]:
        """Return a consistent point-in-time view of both backends."""
        with self._lock:
            return {
                LOCAL: {"used": self._used[LOCAL], "max": self._max[LOCAL]},
                CLOUD: {"used": self._used[CLOUD], "max": self._max[CLOUD]},
            }


# Process-wide singleton. Configure at startup and on settings changes via
# POOL.configure(local_max=..., cloud_max=...).
POOL = _Pool()


if __name__ == "__main__":
    import sys

    failures: list[str] = []

    def check(cond: bool, msg: str) -> None:
        status = "ok  " if cond else "FAIL"
        print(f"[{status}] {msg}")
        if not cond:
            failures.append(msg)

    p = _Pool(local_max=2, cloud_max=4)

    # --- defaults / snapshot accuracy --------------------------------------
    snap = p.snapshot()
    check(snap == {"local": {"used": 0, "max": 2},
                   "cloud": {"used": 0, "max": 4}},
          "initial snapshot reflects constructor maxes, used=0")

    # --- acquire up to the local cap ---------------------------------------
    check(p.try_acquire("local") is True,  "local acquire #1 succeeds")
    check(p.try_acquire("local") is True,  "local acquire #2 succeeds (at cap)")
    check(p.try_acquire("local") is False, "local acquire #3 past cap returns False")
    check(p.snapshot()["local"] == {"used": 2, "max": 2},
          "snapshot shows local used=2/max=2")

    # cloud is independent and still has room
    check(p.try_acquire("cloud") is True, "cloud acquire #1 succeeds (independent pool)")
    check(p.snapshot()["cloud"] == {"used": 1, "max": 4},
          "snapshot shows cloud used=1/max=4")

    # --- release frees a slot, then re-acquire succeeds ---------------------
    p.release("local")
    check(p.snapshot()["local"]["used"] == 1, "release drops local used to 1")
    check(p.try_acquire("local") is True, "re-acquire after release succeeds")
    check(p.snapshot()["local"]["used"] == 2, "local back to used=2")

    # --- used floors at 0 on over-release ----------------------------------
    p.release("cloud")
    p.release("cloud")  # extra release beyond used
    check(p.snapshot()["cloud"]["used"] == 0, "over-release floors cloud used at 0")

    # --- unknown backend is a no-op, never raises --------------------------
    check(p.try_acquire("bogus") is False, "acquire on unknown backend returns False")
    p.release("bogus")  # must not raise
    check(True, "release on unknown backend does not raise")

    # --- configure re-sizes live -------------------------------------------
    # local currently used=2; raise cap to 3 and a new slot becomes available.
    p.configure(local_max=3)
    check(p.snapshot()["local"] == {"used": 2, "max": 3},
          "configure raised local max to 3 (used unchanged)")
    check(p.try_acquire("local") is True, "extra local slot available after resize up")
    check(p.try_acquire("local") is False, "local back at new cap of 3")

    # shrink cloud below... it's at 0 used, so just verify clamp + negative guard
    p.configure(cloud_max=-5)
    check(p.snapshot()["cloud"]["max"] == 0, "negative cloud_max clamps to 0")
    check(p.try_acquire("cloud") is False, "cannot acquire when max clamped to 0")

    # configure(None, None) leaves things untouched
    before = p.snapshot()
    p.configure()
    check(p.snapshot() == before, "configure() with no args is a no-op")

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S)")
        sys.exit(1)
    print("ALL TESTS PASSED")
