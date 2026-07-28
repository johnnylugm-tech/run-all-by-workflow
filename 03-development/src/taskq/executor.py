"""[FR-02, FR-04] taskq executor — task execution and concurrency.

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
  03-development/tests/test_fr04.py:292 — ``executor.run(task_id, use_cache=True)``
    consults ``cache.get`` BEFORE spawning a subprocess and replays a fresh
    entry without invoking ``subprocess.run`` (FR04-AC2-ttl-valid, NFR-09).
  03-development/tests/test_fr04.py:380 — a miss / expired cache falls through
    to the normal execute path, and a successful done result is persisted
    via ``cache.put(sig, result)`` so the NEXT run can replay it
    (FR04-AC3-ttl-missing, FR04-AC4-ttl-expired, FR04-AC5-cache-write).
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from taskq import breaker, cache, store

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
    """[FR-02, FR-04] Truncate to the last ``TAIL_BOUND`` chars.

    Trailing newlines are stripped ONLY when truncation actually occurs
    (text length > TAIL_BOUND). Short outputs are returned verbatim so a
    subsequent cache write preserves the subprocess's own trailing
    newline (FR-04 cache replay must echo the recorded ``stdout_tail``
    byte-for-byte).
    """
    if len(text) > TAIL_BOUND:
        return text.rstrip("\n")[-TAIL_BOUND:]
    return text


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


def _result_payload(outcome: dict) -> dict:
    """[FR-04] Extract the replayable result fields from a terminal outcome.

    Only ``status`` / ``exit_code`` / ``stdout_tail`` / ``stderr_tail``
    are persisted; ``duration_ms`` and ``finished_at`` are deliberately
    recomputed at replay time so the record reflects the REPLAY moment
    rather than the original execution.
    """
    return {
        "status": outcome["status"],
        "exit_code": outcome["exit_code"],
        "stdout_tail": outcome.get("stdout_tail", ""),
        "stderr_tail": outcome.get("stderr_tail", ""),
    }


def _replay_cached(home: Path, task_id: str, command: str) -> bool:
    """[FR-04] Replay a fresh cache entry onto ``task_id``; True on hit.

    A hit writes ``status='done'``, ``cached=True``, and the cached
    ``exit_code`` / ``stdout_tail`` / ``stderr_tail`` onto the task
    record via ``store.update_task`` (atomic tmp + os.replace, NFR-03)
    so the replay is durable across process boundaries (NFR-09).
    """
    entry = cache.get(cache.signature(command))
    if entry is None:
        return False
    cached_result = entry.get("result", {})
    store.update_task(
        home,
        task_id,
        status="done",
        cached=True,
        exit_code=cached_result.get("exit_code", 0),
        stdout_tail=cached_result.get("stdout_tail", ""),
        stderr_tail=cached_result.get("stderr_tail", ""),
        duration_ms=0,
        finished_at=store.now_iso(),
    )
    return True


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


def run(task_id: str, use_cache: bool = False) -> int:
    """[FR-02, FR-04] Execute a single pending task by id, with [FR-03] retry.

    When ``use_cache`` is True, ``taskq.cache.get(signature(command))`` is
    consulted BEFORE any subprocess is spawned. A fresh entry
    (``now - cached_at < TASKQ_CACHE_TTL``) is replayed directly: the
    task transitions to ``done`` with ``cached=True`` and the cached
    ``exit_code`` / ``stdout_tail`` retained, and no subprocess is
    invoked (FR04-AC2-ttl-valid, NFR-09). Missing or expired entries
    fall through to the normal execute path and a successful done
    result is persisted via ``cache.put(sig, result)`` so the next run
    can replay it (FR04-AC3, FR04-AC4, FR04-AC5).

    The circuit breaker is consulted before any subprocess is spawned;
    a failing attempt is retried per ``taskq.breaker``'s policy and the
    final outcome is reported to the breaker.

    Returns:
        ``0`` if the task completed (exit 0 → ``done``, non-zero → ``failed``).
        ``3`` if the breaker is OPEN — no subprocess is spawned (FR-03).
        ``4`` if the task exceeded ``TASKQ_TASK_TIMEOUT`` (SPEC §7).
    """
    home = store.home()
    record = store.load_store(home).get("tasks", {}).get(task_id)
    if record is None:
        return 0

    command = record["command"]

    # [FR-04] Cache hit short-circuits the subprocess — replay the
    # cached result directly onto the task record with cached=True.
    if use_cache and _replay_cached(home, task_id, command):
        return 0

    circuit = breaker.CircuitBreaker(home)
    if not circuit.allow():
        print("breaker open", file=sys.stderr)
        return breaker.EXIT_BREAKER_OPEN

    timeout = int(os.environ.get("TASKQ_TASK_TIMEOUT", str(DEFAULT_TASK_TIMEOUT_SECONDS)))

    # pending → running
    store.update_task(home, task_id, status="running")

    start = time.monotonic()
    outcome = _attempt_with_retry(command, timeout)
    outcome["duration_ms"] = int((time.monotonic() - start) * 1000)
    outcome["finished_at"] = store.now_iso()

    store.update_task(home, task_id, **outcome)

    if _is_terminal_success(outcome):
        circuit.record_success()
        # [FR-04] Persist the successful result so the next
        # ``run --cached`` can replay it without spawning a subprocess.
        cache.put(cache.signature(command), _result_payload(outcome))
    else:
        circuit.record_failure()

    if outcome["status"] == "timeout":
        # Single-task timeout surface a diagnostic to stderr so the
        # operator can see why exit 4 (not exit 0) was returned.
        print(
            f"task {task_id} timed out after {timeout}s",
            file=sys.stderr,
        )
        return EXIT_TIMEOUT
    return 0


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