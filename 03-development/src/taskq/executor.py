"""[FR-02] taskq executor — task execution and concurrency.

Citations:
  03-development/tests/test_fr02.py:163 — ``executor.run(task_id)`` returns int
    (0 for success/non-zero exit, 4 for single-task timeout).
  03-development/tests/test_fr02.py:179 — required result fields
    (``stdout_tail``, ``stderr_tail``, ``duration_ms``, ``finished_at``) are
    recorded on every terminal transition.
  03-development/tests/test_fr02.py:202 — a non-zero exit command reaches
    ``status='failed'`` with the subprocess ``exit_code`` preserved.
  03-development/tests/test_fr02.py:269 — single-task timeout translates to
    OS exit code 4 (SPEC §7) and ``status='timeout'``.
  03-development/tests/test_fr02.py:281 — timeout ``exit_code`` is recorded
    as 4 (SAD §3.1 / SPEC §7).
  03-development/tests/test_fr02.py:316 — ``stdout_tail``/``stderr_tail`` each
    retain at most the last 2000 characters of subprocess output.
  03-development/tests/test_fr02.py:363 — no source file under ``src/taskq``
    ever invokes the shell-bypass flag (the forbidden literal substring);
    ``shlex.split`` is the parsing primitive (NFR-02).
  03-development/tests/test_fr02.py:412 — ``run --all`` fans out via
    ``ThreadPoolExecutor(max_workers=TASKQ_MAX_WORKERS)``; no orphaned
    pending entries remain (NFR-09).
  03-development/tests/test_fr02.py:516 — every concurrent worker write is
    serialised through ``store.STORE_LOCK`` so ``tasks.json`` is never
    corrupted (NFR-08).
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from taskq import breaker, store

# --- Public constants ---------------------------------------------------

TAIL_BOUND = 2000
EXIT_TIMEOUT = 4
DEFAULT_TASK_TIMEOUT_SECONDS = 60
DEFAULT_MAX_WORKERS = 4

# Injection point for the retry backoff (FR-03): tests replace this with
# a recorder so the exponential formula is verified without real waits.
_sleep = time.sleep


# --- Helpers ------------------------------------------------------------

def _tail(text: str) -> str:
    """[FR-02] Strip trailing newlines and keep the last ``TAIL_BOUND`` chars."""
    return text.rstrip("\n")[-TAIL_BOUND:]


def _decode_capture(field) -> str:
    """[FR-02] Normalise ``TimeoutExpired.stdout/stderr`` (bytes | str | None)."""
    if field is None:
        return ""
    if isinstance(field, bytes):
        return field.decode("utf-8", errors="replace")
    return field


def _is_terminal_success(outcome: dict) -> bool:
    """[FR-03] True iff the outcome is a terminal ``done`` (no retry needed)."""
    return outcome.get("status") == "done"


def _execute(command: str, timeout: int) -> dict:
    """[FR-02] Run ``command`` and return the terminal-state record fields.

    Always returns the four fields needed by the run-result update:
    ``status``, ``exit_code``, ``stdout_tail``, ``stderr_tail``. Maps
    ``TimeoutExpired`` to ``status='timeout'`` / ``exit_code=EXIT_TIMEOUT``
    (SPEC §7).
    """
    try:
        result = subprocess.run(
            shlex.split(command),
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "status": "timeout",
            "exit_code": EXIT_TIMEOUT,
            "stdout_tail": _tail(_decode_capture(exc.stdout)),
            "stderr_tail": _tail(_decode_capture(exc.stderr)),
        }

    return {
        "status": "done" if result.returncode == 0 else "failed",
        "exit_code": result.returncode,
        "stdout_tail": _tail(result.stdout),
        "stderr_tail": _tail(result.stderr),
    }


# --- Public API ---------------------------------------------------------

def _attempt_with_retry(command: str, timeout: int) -> dict:
    """[FR-03] Run ``command``, retrying a ``failed``/``timeout`` outcome.

    At most ``TASKQ_RETRY_LIMIT`` attempts are made; before retry ``n`` the
    executor waits ``taskq.breaker.backoff_delay(n)`` seconds through the
    injectable module-level ``_sleep``. The record of the LAST attempt is
    returned.
    """
    attempts = max(1, breaker.retry_limit())
    outcome = _execute(command, timeout)
    for retry_index in range(1, attempts):
        if _is_terminal_success(outcome):
            return outcome
        _sleep(breaker.backoff_delay(retry_index))
        outcome = _execute(command, timeout)
    return outcome


def run(task_id: str) -> int:
    """[FR-02] Execute a single pending task by id, with [FR-03] retry.

    The circuit breaker is consulted before any subprocess is spawned; a
    failing attempt is retried per ``taskq.breaker``'s policy and the final
    outcome is reported to the breaker.

    Returns:
        ``0`` if the task completed (exit 0 → ``done``, non-zero → ``failed``).
        ``3`` if the breaker is OPEN — no subprocess is spawned (FR-03).
        ``4`` if the task exceeded ``TASKQ_TASK_TIMEOUT`` (SPEC §7).
    """
    home = store.home()
    record = store.load_store(home).get("tasks", {}).get(task_id)
    if record is None:
        return 0

    circuit = breaker.CircuitBreaker(home)
    if not circuit.allow():
        print("breaker open", file=sys.stderr)
        return breaker.EXIT_BREAKER_OPEN

    timeout = int(os.environ.get("TASKQ_TASK_TIMEOUT", str(DEFAULT_TASK_TIMEOUT_SECONDS)))

    # pending → running
    store.update_task(home, task_id, status="running")

    start = time.monotonic()
    outcome = _attempt_with_retry(record["command"], timeout)
    outcome["duration_ms"] = int((time.monotonic() - start) * 1000)
    outcome["finished_at"] = store.now_iso()

    store.update_task(home, task_id, **outcome)

    if _is_terminal_success(outcome):
        circuit.record_success()
    else:
        circuit.record_failure()

    return EXIT_TIMEOUT if outcome["status"] == "timeout" else 0


def run_all() -> int:
    """[FR-02] Execute every pending task concurrently.

    Fans out via ``ThreadPoolExecutor(max_workers=TASKQ_MAX_WORKERS)``.
    Every worker write is serialised on ``store.STORE_LOCK`` (NFR-08), so
    all pending entries transition to a terminal state without loss or
    corruption (NFR-09).
    """
    home = store.home()
    pending_ids = [
        tid
        for tid, rec in store.load_store(home).get("tasks", {}).items()
        if rec.get("status") == "pending"
    ]
    if not pending_ids:
        return 0

    max_workers = int(os.environ.get("TASKQ_MAX_WORKERS", str(DEFAULT_MAX_WORKERS)))

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(run, tid) for tid in pending_ids]
        for fut in futures:
            fut.result()

    return 0