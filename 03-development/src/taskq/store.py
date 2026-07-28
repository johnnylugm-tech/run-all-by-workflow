"""[FR-01, FR-02] taskq storage — tasks.json read/write + concurrency lock.

Single source of truth for the on-disk task queue and the lock that
serialises every read-modify-write across the worker pool.

Citations:
  03-development/tests/test_fr01.py:248 — root dict shape is
    ``{version, tasks}``; loader returns the v1 skeleton when absent.
  03-development/tests/test_fr01.py:295 — NFR-03 reliability: tmp +
    os.replace leaves tasks.json valid and clean (no orphan tmp / ~ files).
  03-development/tests/test_fr02.py:516 — every concurrent worker write
    is serialised through ``STORE_LOCK`` so tasks.json is never corrupted
    (NFR-08).
"""

from __future__ import annotations

import fcntl
import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

TASKS_FILENAME = "tasks.json"
TASKS_LOCK_FILENAME = ".tasks.json.lock"

# Module-level lock serialises every read-modify-write of tasks.json
# across the ThreadPoolExecutor worker pool (NFR-08).
STORE_LOCK = threading.Lock()


def home() -> Path:
    """[FR-01] Resolve the TASKQ_HOME directory from the environment."""
    raw = os.environ.get("TASKQ_HOME")
    return Path(raw) if raw else Path.home() / ".taskq"


def now_iso() -> str:
    """[FR-01] Current UTC timestamp in ISO-8601."""
    return datetime.now(timezone.utc).isoformat()


def load_store(home: Path) -> dict:
    """[FR-01] Read tasks.json from ``home``; return v1 skeleton if absent."""
    path = home / TASKS_FILENAME
    if not path.exists():
        return {"version": 1, "tasks": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def _file_lock(path: Path):
    """Acquire an OS-level exclusive ``flock`` on ``path`` for the
    duration of the ``with`` block (NFR-08 cross-process serialisation).

    Independent ``python -m taskq`` processes sharing ``$TASKQ_HOME``
    would otherwise race on tasks.json's read-modify-write — ``STORE_LOCK``
    is a ``threading.Lock`` and only protects within a single process.
    Callers MUST use this around any read-modify-write that they cannot
    fold into a single ``_atomic_write_json`` call (e.g. ``_cmd_submit``
    which checks name uniqueness BEFORE writing).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.parent / f".{path.name}.lock"
    return _FileLock(lock_path)


class _FileLock:
    """``flock``-based context manager. Acquired exclusive on enter,
    released on exit (including exception)."""

    def __init__(self, lock_path: Path) -> None:
        self._lock_path = lock_path
        self._handle = None  # type: ignore[var-annotated]

    def __enter__(self):
        self._handle = open(self._lock_path, "w", encoding="utf-8")
        fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._handle is not None:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()


def _atomic_write_json(path: Path, data: dict) -> None:
    """Atomic JSON write — tmp + os.replace (NFR-03).

    Callers MUST hold ``_file_lock(path)`` for the duration of any
    read-modify-write that ends in this call, otherwise two processes
    can pick the same tmp filename and the ``finally: tmp.unlink()``
    cleanup races the OTHER process's pending write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # 16 hex chars → 64 bits of randomness, making the chance of two
    # concurrent writers picking the same tmp suffix negligible.
    tmp = path.parent / f".{path.name}.{uuid.uuid4().hex[:16]}.tmp"
    try:
        tmp.write_text(
            json.dumps(data, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(tmp, path)
    finally:
        # Only unlink the tmp file we OW this process — by construction
        # we are the sole holder of the flock so no other writer's tmp
        # file can share this path.
        if tmp.exists():
            tmp.unlink()


def update_task(home: Path, task_id: str, **fields) -> None:
    """[FR-02] Lock-protected read-modify-write of a single task record.

    Concurrent worker threads serialise on ``STORE_LOCK`` and concurrent
    processes serialise on the cross-process ``_file_lock`` so updates
    never interleave and ``tasks.json`` is never corrupted (NFR-08).
    No-op when ``task_id`` is not present in the store.
    """
    with STORE_LOCK, _file_lock(home / TASKS_FILENAME):
        store = load_store(home)
        tasks = store.get("tasks", {})
        if task_id in tasks:
            tasks[task_id].update(fields)
            _atomic_write_json(home / TASKS_FILENAME, store)