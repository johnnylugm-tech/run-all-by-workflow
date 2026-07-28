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
    serialised through a shared ``threading.Lock`` so ``tasks.json`` is
    never corrupted (NFR-08).
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

# --- Public constants ---------------------------------------------------

TAIL_BOUND = 2000
EXIT_TIMEOUT = 4
TASKS_FILENAME = "tasks.json"

# Module-level lock serialises every read-modify-write of tasks.json
# across the ThreadPoolExecutor worker pool (NFR-08).
_STORE_LOCK = threading.Lock()


# --- Helpers ------------------------------------------------------------

def _home() -> Path:
    """[FR-02] Resolve the TASKQ_HOME directory from the environment."""
    raw = os.environ.get("TASKQ_HOME")
    return Path(raw) if raw else Path.home() / ".taskq"


def _now_iso() -> str:
    """[FR-02] Current UTC timestamp in ISO-8601."""
    return datetime.now(timezone.utc).isoformat()


def _load_store(home: Path) -> dict:
    """[FR-02] Read tasks.json from ``home``; return v1 skeleton if absent."""
    path = home / TASKS_FILENAME
    if not path.exists():
        return {"version": 1, "tasks": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def _atomic_write_json(path: Path, data: dict) -> None:
    """[FR-02] Atomic JSON write — tmp + os.replace (NFR-03)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{uuid.uuid4().hex[:8]}.tmp"
    try:
        tmp.write_text(
            json.dumps(data, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _update_task(home: Path, task_id: str, **fields) -> None:
    """[FR-02] Lock-protected read-modify-write of a task record.

    Concurrent worker threads serialise on ``_STORE_LOCK`` so updates never
    interleave and ``tasks.json`` is never corrupted (NFR-08).
    """
    with _STORE_LOCK:
        store = _load_store(home)
        tasks = store.get("tasks", {})
        if task_id in tasks:
            tasks[task_id].update(fields)
            _atomic_write_json(home / TASKS_FILENAME, store)


def _decode_timeout(field) -> str:
    """[FR-02] Normalise ``TimeoutExpired.stdout/stderr`` (bytes | str | None)."""
    if field is None:
        return ""
    if isinstance(field, bytes):
        return field.decode("utf-8", errors="replace")
    return field


# --- Public API ---------------------------------------------------------

def run(task_id: str) -> int:
    """[FR-02] Execute a single pending task by id.

    Returns:
        ``0`` if the task completed (exit 0 → ``done``, non-zero → ``failed``).
        ``4`` if the task exceeded ``TASKQ_TASK_TIMEOUT`` (SPEC §7).
    """
    home = _home()
    store = _load_store(home)
    record = store.get("tasks", {}).get(task_id)
    if record is None:
        return 0

    command = record["command"]
    timeout = int(os.environ.get("TASKQ_TASK_TIMEOUT", "60"))

    # pending → running
    _update_task(home, task_id, status="running")

    start = time.monotonic()
    status: str
    exit_code: int
    stdout_tail: str
    stderr_tail: str
    try:
        result = subprocess.run(
            shlex.split(command),
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        exit_code = result.returncode
        status = "done" if exit_code == 0 else "failed"
        stdout_tail = result.stdout.rstrip("\n")[-TAIL_BOUND:]
        stderr_tail = result.stderr.rstrip("\n")[-TAIL_BOUND:]
    except subprocess.TimeoutExpired as exc:
        exit_code = EXIT_TIMEOUT
        status = "timeout"
        stdout_tail = _decode_timeout(exc.stdout).rstrip("\n")[-TAIL_BOUND:]
        stderr_tail = _decode_timeout(exc.stderr).rstrip("\n")[-TAIL_BOUND:]

    duration_ms = int((time.monotonic() - start) * 1000)

    _update_task(
        home,
        task_id,
        status=status,
        exit_code=exit_code,
        stdout_tail=stdout_tail,
        stderr_tail=stderr_tail,
        duration_ms=duration_ms,
        finished_at=_now_iso(),
    )

    return EXIT_TIMEOUT if status == "timeout" else 0


def run_all() -> int:
    """[FR-02] Execute every pending task concurrently.

    Fans out via ``ThreadPoolExecutor(max_workers=TASKQ_MAX_WORKERS)``.
    Every worker write is serialised on ``_STORE_LOCK`` (NFR-08), so all
    pending entries transition to a terminal state without loss or
    corruption (NFR-09).
    """
    home = _home()
    store = _load_store(home)
    pending_ids = [
        tid
        for tid, rec in store.get("tasks", {}).items()
        if rec.get("status") == "pending"
    ]
    if not pending_ids:
        return 0

    max_workers = int(os.environ.get("TASKQ_MAX_WORKERS", "4"))

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(run, tid) for tid in pending_ids]
        for fut in futures:
            fut.result()

    return 0