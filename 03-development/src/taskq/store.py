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

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

TASKS_FILENAME = "tasks.json"

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


def _atomic_write_json(path: Path, data: dict) -> None:
    """Atomic JSON write — tmp + os.replace (NFR-03)."""
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
            tmp.unlink()  # atomic-write cleanup: fires when os.replace fails before tmp is renamed


def update_task(home: Path, task_id: str, **fields) -> None:
    """[FR-02] Lock-protected read-modify-write of a single task record.

    Concurrent worker threads serialise on ``STORE_LOCK`` so updates
    never interleave and ``tasks.json`` is never corrupted (NFR-08).
    No-op when ``task_id`` is not present in the store.
    """
    with STORE_LOCK:
        store = load_store(home)
        tasks = store.get("tasks", {})
        if task_id in tasks:
            tasks[task_id].update(fields)
            _atomic_write_json(home / TASKS_FILENAME, store)