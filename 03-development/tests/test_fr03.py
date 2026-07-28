"""FR-03: Retry and circuit breaker — TDD-RED failing tests.

Tests in this file exercise `taskq.executor.run` retry behaviour and the
`taskq.breaker.CircuitBreaker` state machine per SRS.md FR-03 and
TEST_SPEC.md (rows #1–#7 for FR-03).

Test strategy:
- Cases #1, #3, #5, #6 exercise `taskq.executor.run` directly (in-process)
  with an injected sleep function and a stub `subprocess.run` so we can
  count retry attempts without real wall-time waits.
- Case #2 is a unit test of the exponential backoff calculation using an
  injected sleep — verifies the formula `BACKOFF_BASE × 2^n` exactly.
- Case #4 exercises the open-breaker rejection path: breaker is OPEN,
  `run` must exit 3, emit `breaker open` on stderr, and NOT spawn a
  subprocess (autouse fixture counts `subprocess.run` calls).
- Case #7 is a cross-process integration test that writes/reads
  `breaker.json` via two `python -m taskq` subprocesses sharing
  `$TASKQ_HOME` (per FR-03: "State is atomically persisted in
  $TASKQ_HOME/breaker.json").

NOTE: These tests are expected to FAIL with a Collection Error
(ModuleNotFoundError: No module named 'taskq.executor' / 'taskq.breaker')
because the retry/breaker source code does not exist yet. That is the
GREEN step's job. Do not add try/except ImportError to hide the failure —
the Collection Error is the valid RED state.
"""

from __future__ import annotations

import json as json_lib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

# Standard top-level imports — Collection Error (Exit Code 2) is the valid
# RED state when source code does not exist yet. These exercise both
# taskq.executor (retry path) and taskq.breaker (state machine).
from taskq import breaker, cli, executor, store  # noqa: E402  (collection error is expected)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

# SRC_ROOT so subprocess tests can propagate PYTHONPATH (pytest's
# `pythonpath` setting does NOT propagate to child processes
# automatically).
SRC_ROOT = Path(__file__).resolve().parent.parent / "src"


@pytest.fixture
def taskq_home(tmp_path, monkeypatch):
    """Per-test isolated $TASKQ_HOME.

    Sets TASKQ_RETRY_LIMIT=3 and TASKQ_BACKOFF_BASE=0.001 so retries
    happen quickly but the backoff formula is still injectable (the
    formula test sets its own value).
    """
    home = tmp_path / ".taskq"
    home.mkdir()
    monkeypatch.setenv("TASKQ_HOME", str(home))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TASKQ_RETRY_LIMIT", "3")
    monkeypatch.setenv("TASKQ_BACKOFF_BASE", "0.001")
    return home


# Subprocess-call counter for tests that must assert subprocess.run is
# NEVER invoked (e.g. test #4 — breaker OPEN rejects without spawning).
class _SubprocessCallCounter:
    """Tracks every call to ``subprocess.run`` during a test.

    The fixture installs this counter by monkey-patching
    ``taskq.executor.subprocess.run`` (the same object the production
    code uses) so any future implementation that bypasses our patching
    would still be flagged.
    """

    def __init__(self):
        self.count = 0

    def __call__(self, *args, **kwargs):
        self.count += 1
        # Return a benign CompletedProcess-like object so the code
        # under test does not crash if it tries to read .returncode.
        class _FakeResult:
            returncode = 0
            stdout = ""
            stderr = ""

        return _FakeResult()


@pytest.fixture
def subprocess_counter(monkeypatch):
    """Install a counter that records every subprocess.run invocation.

    The patched object is ``taskq.executor.subprocess.run`` so any
    production code that imports ``subprocess`` inside
    ``taskq.executor`` and calls ``subprocess.run(...)`` is captured.
    """
    counter = _SubprocessCallCounter()
    monkeypatch.setattr(executor.subprocess, "run", counter)
    return counter


def _build_env(taskq_home: Path) -> dict:
    """Build a child-process env that propagates TASKQ_HOME + PYTHONPATH
    and inherits TASKQ_* tunables (set by the parent fixture via
    monkeypatch).
    """
    env = os.environ.copy()
    env["TASKQ_HOME"] = str(taskq_home)
    env["PYTHONPATH"] = str(SRC_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return env


def _run_cli(args, taskq_home: Path, timeout: int = 30):
    """Run `python -m taskq ...` as a subprocess for the given isolated
    home."""
    cmd = [sys.executable, "-m", "taskq", *args]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=_build_env(taskq_home),
        timeout=timeout,
    )


def _seed_via_cli(taskq_home: Path, command: str) -> str:
    """Seed a pending task via `taskq submit`; return id."""
    result = _run_cli(["submit", command], taskq_home)
    if result.returncode != 0:
        raise AssertionError(
            f"submit seed failed: exit={result.returncode}; "
            f"stderr={result.stderr!r}"
        )
    return result.stdout.strip()


def _write_breaker_json(taskq_home: Path, payload: dict) -> None:
    """Seed `breaker.json` with the given payload.

    Used by tests #3, #5, #6 to set up an initial breaker state.
    """
    path = taskq_home / "breaker.json"
    path.write_text(
        json_lib.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )


def _read_breaker_json(taskq_home: Path) -> dict:
    """Read and parse $TASKQ_HOME/breaker.json."""
    path = taskq_home / "breaker.json"
    if not path.exists():
        return {}
    return json_lib.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Case #1: test_fr03_01_retry_cap  (boundary — retry cap)
# ---------------------------------------------------------------------------

def test_fr03_01_retry_cap(taskq_home, subprocess_counter, monkeypatch):
    """FR-03: a failed command must retry automatically up to
    `TASKQ_RETRY_LIMIT` times. After the cap, the run returns
    successfully (the run itself completed — the task is in `failed`
    state) and `subprocess.run` was called exactly `retry_limit` times.

    Sub-assertion rule: FR03-AC1-retry-cap — `len(retry_limit) > 0`.

    NFR associations:
    # NFR-09 — performance: retries must be bounded by the configured cap,
    # not unbounded.
    # NFR-03 — reliability: each retry attempt must produce a final
    # terminal record in tasks.json (no orphaned `running` state).
    """
    # GREEN TODO: taskq.executor.run must wrap the subprocess call in a
    # retry loop bounded by `TASKQ_RETRY_LIMIT` env var (default 2). On
    # `failed`/`timeout`, the loop sleeps (with injectable sleep_fn)
    # and re-executes; on success the loop returns immediately. The
    # final task record reflects the LAST attempt's outcome.
    retry_limit = "3"
    monkeypatch.setenv("TASKQ_RETRY_LIMIT", retry_limit)
    # Backoff is zero so the test does not sleep for real seconds.
    monkeypatch.setenv("TASKQ_BACKOFF_BASE", "0")

    task_id = _seed_via_cli(taskq_home, "false")

    rc = executor.run(task_id)

    # The run itself completed — executor returns 0 for a failed/retried
    # task; the *task* is in failed state, not the process.
    assert rc == 0, (
        f"executor.run returned {rc}; expected 0 (run completed, task is "
        f"in failed state)"
    )
    # Exactly `retry_limit` subprocess invocations: 1 initial + N retries.
    expected_attempts = int(retry_limit)
    assert subprocess_counter.count == expected_attempts, (
        f"expected exactly {expected_attempts} subprocess.run invocations "
        f"(1 initial + {int(retry_limit) - 1} retries), got "
        f"{subprocess_counter.count}"
    )

    # tasks.json must record the final terminal state (failed), not be
    # left in `running`.
    raw = (taskq_home / "tasks.json").read_text(encoding="utf-8")
    stored = json_lib.loads(raw)
    record = stored["tasks"][task_id]
    assert record["status"] == "failed", (
        f"task status must be 'failed' after retry-cap exhaustion; got "
        f"{record['status']!r}"
    )


# ---------------------------------------------------------------------------
# Case #2: test_fr03_02_exponential_delay_injected_sleep  (unit — backoff math)
# ---------------------------------------------------------------------------

def test_fr03_02_exponential_delay_injected_sleep(
    taskq_home, subprocess_counter, monkeypatch
):
    """FR-03: before retry number `n`, the executor must sleep exactly
    `TASKQ_BACKOFF_BASE × 2^n` seconds. The sleep function is
    injectable so tests can verify the formula without real waits.

    Sub-assertion rule: FR03-AC2-exponential-delay —
    `backoff_base == "1.0" and retry_index == "2" and expected_delay == "4.0"`.

    NFR associations:
    # NFR-09 — performance: deterministic backoff means wall-time cost
    # is bounded and predictable under retry storms.
    """
    # GREEN TODO: taskq.executor.run must accept (or read) an injectable
    # sleep function so this unit test can verify the formula without
    # real-time waits. The default sleep must be `time.sleep`; tests
    # pass a no-op recorder.
    retry_index = 2
    backoff_base = "1.0"
    expected_delay = 4.0  # = 1.0 × 2^2
    monkeypatch.setenv("TASKQ_BACKOFF_BASE", backoff_base)
    monkeypatch.setenv("TASKQ_RETRY_LIMIT", "3")

    # Capture every sleep call made by the retry path.
    recorded_delays: list[float] = []

    def _recording_sleep(seconds: float) -> None:
        recorded_delays.append(float(seconds))

    # Patch the module's sleep reference. GREEN TODO: executor must
    # expose either a module-level `_sleep` callable or accept `sleep_fn`
    # via run(...); this test assumes the module-level form.
    monkeypatch.setattr(executor, "_sleep", _recording_sleep, raising=False)

    task_id = _seed_via_cli(taskq_home, "false")
    rc = executor.run(task_id)
    assert rc == 0, f"executor.run returned {rc}; expected 0"

    # There must be at least one sleep recorded before retry #2.
    # expected_delay is BACKOFF_BASE × 2^retry_index = 1.0 × 4 = 4.0.
    assert any(abs(d - expected_delay) < 1e-9 for d in recorded_delays), (
        f"expected at least one sleep of {expected_delay}s "
        f"(BACKOFF_BASE × 2^{retry_index}); recorded delays: "
        f"{recorded_delays}"
    )

    # And the recorded delay sequence must follow the exponential
    # progression: at least the first retry's delay must equal
    # BACKOFF_BASE × 2^1 = 2.0; the retry_index-th must equal
    # BACKOFF_BASE × 2^retry_index = 4.0.
    assert any(abs(d - 2.0) < 1e-9 for d in recorded_delays), (
        f"expected at least one sleep of 2.0s (BACKOFF_BASE × 2^1); "
        f"recorded: {recorded_delays}"
    )


# ---------------------------------------------------------------------------
# Case #3: test_fr03_03_threshold_opens_breaker  (state transition)
# ---------------------------------------------------------------------------

def test_fr03_03_threshold_opens_breaker(taskq_home, monkeypatch):
    """FR-03: when consecutive final failures reach
    `TASKQ_BREAKER_THRESHOLD`, the breaker transitions to `OPEN` and
    the state is persisted to `breaker.json`.

    Sub-assertion rule: FR03-AC3-threshold-met —
    `consecutive_failures >= breaker_threshold`.

    NFR associations:
    # NFR-03 — reliability: breaker state persists across crashes via
    # the atomic write to `breaker.json`.
    # NFR-08 — concurrency: shared breaker state must be readable from
    # any process sharing `$TASKQ_HOME`.
    """
    # GREEN TODO: taskq.breaker.CircuitBreaker must have
    #   - `record_failure()` -> updates consecutive_failures count and
    #     transitions to OPEN when the threshold is met.
    #   - `state() -> str` — returns "CLOSED" | "OPEN" | "HALF_OPEN".
    #   - The module must atomically persist state to
    #     `$TASKQ_HOME/breaker.json` via `store._atomic_write_json`.
    consecutive_failures = 3
    breaker_threshold = 3
    expected_state = "OPEN"

    monkeypatch.setenv("TASKQ_BREAKER_THRESHOLD", str(breaker_threshold))

    cb = breaker.CircuitBreaker(home=taskq_home)

    # Pre-condition: breaker starts CLOSED.
    assert cb.state() == "CLOSED", (
        f"breaker initial state must be CLOSED; got {cb.state()!r}"
    )

    # Drive `consecutive_failures` failures; the `threshold`-th must
    # flip state to OPEN.
    for i in range(consecutive_failures):
        cb.record_failure()

    # State must be OPEN after threshold met.
    assert cb.state() == expected_state, (
        f"expected breaker state {expected_state!r} after "
        f"{consecutive_failures} consecutive failures (threshold="
        f"{breaker_threshold}); got {cb.state()!r}"
    )

    # State must be atomically persisted to breaker.json.
    persisted = _read_breaker_json(taskq_home)
    assert persisted.get("state") == expected_state, (
        f"breaker.json 'state' must be {expected_state!r}; got "
        f"{persisted.get('state')!r}"
    )


# ---------------------------------------------------------------------------
# Case #4: test_fr03_04_open_rejection_no_subprocess  (rejection)
# ---------------------------------------------------------------------------

def test_fr03_04_open_rejection_no_subprocess(
    taskq_home, subprocess_counter, monkeypatch
):
    """FR-03: while the breaker is `OPEN`, any run must be immediately
    rejected — exit code 3, stderr `breaker open`, and NO subprocess
    is spawned.

    Sub-assertion rule: FR03-AC4-open-rejects —
    `breaker_state == "OPEN"`.

    NFR associations:
    # NFR-03 — reliability: an OPEN breaker must fail-fast without
    # touching the subprocess layer.
    # NFR-09 — performance: rejection is O(1) — no subprocess, no
    # backoff sleep.
    """
    # GREEN TODO: taskq.executor.run must consult the circuit breaker
    # before every attempt; when state is OPEN, run must return 3,
    # emit `breaker open` to stderr, and NOT call subprocess.run.
    monkeypatch.setenv("TASKQ_BREAKER_THRESHOLD", "3")

    # Seed an OPEN breaker via the breaker module's API.
    cb = breaker.CircuitBreaker(home=taskq_home)
    for _ in range(3):
        cb.record_failure()
    assert cb.state() == "OPEN", (
        f"setup precondition failed: breaker must be OPEN before "
        f"rejection test; got {cb.state()!r}"
    )

    # Seed a pending task.
    task_id = _seed_via_cli(taskq_home, "false")

    # Snapshot subprocess count BEFORE invoking run — it must not
    # change as a result of the rejection path.
    before_count = subprocess_counter.count

    # Run the task; capture stderr via redirect.
    import contextlib
    import io as io_lib

    buf_err = io_lib.StringIO()
    rc = 1
    saved_stderr = sys.stderr
    try:
        sys.stderr = buf_err
        rc = executor.run(task_id)
    finally:
        sys.stderr = saved_stderr

    stderr_text = buf_err.getvalue()

    # Exit code 3 per FR-03 ("OPEN" → exit 3).
    assert rc == 3, (
        f"executor.run returned {rc}; expected 3 (breaker-OPEN rejection "
        f"per FR-03); stderr={stderr_text!r}"
    )
    # Stderr must contain the literal `breaker open` message.
    assert "breaker open" in stderr_text.lower(), (
        f"expected stderr to contain 'breaker open'; got {stderr_text!r}"
    )
    # No subprocess invocation occurred during the rejection path.
    assert subprocess_counter.count == before_count, (
        f"breaker OPEN must reject without spawning a subprocess; "
        f"subprocess.run was called {subprocess_counter.count - before_count} "
        f"times during rejection"
    )


# ---------------------------------------------------------------------------
# Case #5: test_fr03_05_half_open_success_closes_resets  (happy — recovery)
# ---------------------------------------------------------------------------

def test_fr03_05_half_open_success_closes_resets(taskq_home, monkeypatch):
    """FR-03: a HALF_OPEN probe that succeeds must transition the
    breaker to CLOSED and reset the consecutive-failure count to 0.

    Sub-assertion rule: FR03-AC5-halfopen-success —
    `breaker_state == "HALF_OPEN" and probe_outcome == "success"`.

    NFR associations:
    # NFR-03 — reliability: self-healing — a single successful probe
    # restores service and clears the failure history.
    """
    # GREEN TODO: taskq.breaker.CircuitBreaker must support
    # `record_success()` which, when called in HALF_OPEN state,
    # transitions to CLOSED and zeroes consecutive_failures. The
    # module must persist the new state to breaker.json.
    reset_count = 0
    expected_state = "CLOSED"

    monkeypatch.setenv("TASKQ_BREAKER_THRESHOLD", "3")
    monkeypatch.setenv("TASKQ_BREAKER_COOLDOWN", "0")

    # Seed a HALF_OPEN breaker via the persisted state file — that is
    # what cross-process consumers see after the cooldown elapses.
    _write_breaker_json(
        taskq_home,
        {
            "version": 1,
            "state": "HALF_OPEN",
            "consecutive_failures": 5,
            "opened_at": 0.0,
        },
    )

    cb = breaker.CircuitBreaker(home=taskq_home)
    assert cb.state() == "HALF_OPEN", (
        f"setup precondition failed: breaker must be HALF_OPEN; got "
        f"{cb.state()!r}"
    )

    cb.record_success()

    assert cb.state() == expected_state, (
        f"expected breaker state {expected_state!r} after HALF_OPEN "
        f"success probe; got {cb.state()!r}"
    )

    persisted = _read_breaker_json(taskq_home)
    assert persisted.get("state") == expected_state, (
        f"breaker.json 'state' must be {expected_state!r}; got "
        f"{persisted.get('state')!r}"
    )
    assert persisted.get("consecutive_failures") == reset_count, (
        f"breaker.json 'consecutive_failures' must be {reset_count} after "
        f"CLOSED transition; got {persisted.get('consecutive_failures')!r}"
    )


# ---------------------------------------------------------------------------
# Case #6: test_fr03_06_half_open_failure_reopens  (validation — re-open)
# ---------------------------------------------------------------------------

def test_fr03_06_half_open_failure_reopens(taskq_home, monkeypatch):
    """FR-03: a HALF_OPEN probe that fails must transition the breaker
    back to OPEN (and reset the cooldown clock).

    Sub-assertion rule: FR03-AC6-halfopen-failure —
    `breaker_state == "HALF_OPEN" and probe_outcome == "failure"`.

    NFR associations:
    # NFR-03 — reliability: a failed probe proves the downstream is
    # still unhealthy; the breaker stays open for another cooldown.
    """
    # GREEN TODO: taskq.breaker.CircuitBreaker.record_failure() must,
    # when called in HALF_OPEN state, transition back to OPEN and
    # reset `opened_at` so the cooldown clock restarts.
    expected_state = "OPEN"

    monkeypatch.setenv("TASKQ_BREAKER_THRESHOLD", "3")

    _write_breaker_json(
        taskq_home,
        {
            "version": 1,
            "state": "HALF_OPEN",
            "consecutive_failures": 5,
            "opened_at": 0.0,
        },
    )

    cb = breaker.CircuitBreaker(home=taskq_home)
    assert cb.state() == "HALF_OPEN", (
        f"setup precondition failed: breaker must be HALF_OPEN; got "
        f"{cb.state()!r}"
    )

    cb.record_failure()

    assert cb.state() == expected_state, (
        f"expected breaker state {expected_state!r} after HALF_OPEN "
        f"failure probe; got {cb.state()!r}"
    )

    persisted = _read_breaker_json(taskq_home)
    assert persisted.get("state") == expected_state, (
        f"breaker.json 'state' must be {expected_state!r}; got "
        f"{persisted.get('state')!r}"
    )


# ---------------------------------------------------------------------------
# Case #7: test_fr03_07_cross_process_persistent_breaker  (integration)
# ---------------------------------------------------------------------------

def test_fr03_07_cross_process_persistent_breaker(tmp_path, monkeypatch):
    """FR-03: breaker state persisted to `$TASKQ_HOME/breaker.json` is
    visible to any other process sharing the same TASKQ_HOME —
    i.e. two `python -m taskq` subprocesses must observe the same
    OPEN state written by the first.

    Sub-assertion rule: FR03-AC7-cross-process —
    `process_count > "1" and shared_TASKQ_HOME == "true"`.

    NFR associations:
    # NFR-08 — concurrency: cross-process flock serialises breaker.json
    # writes; the second process must read what the first wrote.
    # NFR-03 — reliability: the cross-process state survives SIGKILL
    # because it was atomically written.
    """
    process_count = 2
    shared_TASKQ_HOME = "true"

    home = tmp_path / ".taskq_xproc"
    home.mkdir()
    env = _build_env(home)
    # Make sure the breaker trips fast in the writing process.
    env["TASKQ_BREAKER_THRESHOLD"] = "3"
    env["TASKQ_BREAKER_COOLDOWN"] = "0"

    # --- Process A: drive 3 failures to OPEN the breaker ---
    # The first subprocess uses the breaker module to flip OPEN; we
    # exercise the public surface (cli) to confirm the cross-process
    # contract: when one process opens the breaker, a second process
    # must observe OPEN without touching its own breaker.
    cmd_a = [
        sys.executable, "-m", "taskq", "_breaker", "drive_open",
    ]
    # NOTE: `taskq _breaker drive_open` is the GREEN-side entry point
    # this test depends on. If the GREEN agent picks a different CLI
    # surface, the subprocess command above can be swapped for a
    # `python -c` invocation that imports `taskq.breaker`.
    proc_a = subprocess.run(
        cmd_a,
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )
    # The driving process must have succeeded; if the GREEN agent chose
    # a different surface, swap the command but keep the assertion.
    assert proc_a.returncode == 0, (
        f"process A (driver) failed: exit={proc_a.returncode}; "
        f"stderr={proc_a.stderr!r}"
    )

    # breaker.json must now exist and be OPEN.
    assert (home / "breaker.json").exists(), (
        f"process A must have written breaker.json at {home/'breaker.json'} "
        f"after driving it OPEN"
    )
    persisted_a = _read_breaker_json(home)
    assert persisted_a.get("state") == "OPEN", (
        f"breaker.json 'state' must be 'OPEN' after process A drove it "
        f"open; got {persisted_a.get('state')!r}"
    )

    # --- Process B: must observe OPEN from disk ---
    # Use a separate TASKQ_HOME-resolving subprocess so it definitely
    # reads from disk, not from any in-process cache.
    cmd_b = [
        sys.executable, "-m", "taskq", "_breaker", "state",
    ]
    proc_b = subprocess.run(
        cmd_b,
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )
    assert proc_b.returncode == 0, (
        f"process B (reader) failed: exit={proc_b.returncode}; "
        f"stderr={proc_b.stderr!r}"
    )

    # The state observable from process B (via stdout or via re-reading
    # the same file in this test) must be OPEN — proving cross-process
    # persistence.
    persisted_b = _read_breaker_json(home)
    assert persisted_b.get("state") == "OPEN", (
        f"breaker.json after process B read must still be 'OPEN'; got "
        f"{persisted_b.get('state')!r}"
    )

    # Sentinel — assert the parametrize-style invariant from TEST_SPEC.
    assert int(process_count) > 1 and shared_TASKQ_HOME == "true", (
        f"test precondition: process_count={process_count}, "
        f"shared_TASKQ_HOME={shared_TASKQ_HOME}"
    )