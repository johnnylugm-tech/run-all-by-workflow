"""[FR-04] taskq cache — TTL-bounded result cache.

SHA-256-keyed result cache persisted to ``$TASKQ_HOME/cache.json`` per
SPEC §5.2 schema (``{version: 1, entries: {sig: {result, cached_at}}}``).
Reads and writes are serialised on ``store.STORE_LOCK`` so concurrent
threads never observe a torn JSON document (NFR-08 / NP-07).

Citations:
  03-development/tests/test_fr04.py:196 — ``cache.signature(command)``
    returns the SHA-256 hex digest of the UTF-8-encoded command,
    deterministically (FR04-AC1-signature-input).
  03-development/tests/test_fr04.py:265 — ``cache.get(sig)`` returns the
    cached entry dict when present and fresh (FR04-AC2-ttl-valid); None
    when missing or expired (FR04-AC3-ttl-missing, FR04-AC4-ttl-expired).
  03-development/tests/test_fr04.py:439 — ``cache.put(sig, result)``
    persists an entry with ``cached_at`` epoch timestamp
    (FR04-AC5-cache-write).
  03-development/tests/test_fr04.py:600 — concurrent readers/writers
    share the lock and the persisted document remains a valid JSON
    object with the v1 schema (FR04-AC6-concurrent-cache, NFR-08).
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from taskq import store

# --- Public constants ---------------------------------------------------

CACHE_FILENAME = "cache.json"
DEFAULT_CACHE_TTL_SECONDS = 60


# --- Configuration (NFR-06: every knob is an env var with a default) ----

def _ttl_seconds() -> int:
    """[FR-04] Resolve the cache TTL (seconds) from the environment."""
    return int(os.environ.get("TASKQ_CACHE_TTL", str(DEFAULT_CACHE_TTL_SECONDS)))


def _cache_path() -> Path:
    """[FR-04] Location of the persisted cache document under TASKQ_HOME."""
    return store.home() / CACHE_FILENAME


# --- Persistence --------------------------------------------------------

def _load() -> dict:
    """[FR-04] Read cache.json; return the v1 skeleton when absent.

    A missing file is the legitimate cold-start case; a file that exists
    but does not parse is surfaced as a ``JSONDecodeError`` (NFR-07 fail
    fast) rather than silently rebuilt.
    """
    path = _cache_path()
    if not path.exists():
        return {"version": 1, "entries": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def _save(data: dict) -> None:
    """[FR-04] Atomically persist the cache document (NFR-03 tmp + replace)."""
    data["version"] = 1
    store._atomic_write_json(_cache_path(), data)


def _make_entry(result: dict) -> dict:
    """[FR-04] Wrap a result payload in a fresh cache entry (SPEC §5.2).

    ``dict(result)`` copies so a later in-place mutation of the caller's
    dict does not silently rewrite the persisted entry.
    """
    return {"result": dict(result), "cached_at": time.time()}


# --- Public API ---------------------------------------------------------

def signature(command: str) -> str:
    """[FR-04] SHA-256 hex digest of the UTF-8-encoded command.

    The signature is deterministic across processes and platforms so a
    replay short-circuit can hash the same command and observe the same
    entry (NFR-10).
    """
    return hashlib.sha256(command.encode("utf-8")).hexdigest()


def get(sig: str) -> dict | None:
    """[FR-04] Return the cache entry for ``sig`` if present and fresh.

    ``None`` is returned when the entry is missing OR its age exceeds
    ``TASKQ_CACHE_TTL`` seconds. The full entry shape
    ``{result: {...}, cached_at: float}`` is returned otherwise so the
    caller can replay every recorded field.
    """
    with store.STORE_LOCK:
        entry = _load().get("entries", {}).get(sig)
        if entry is None:
            return None
        if (time.time() - float(entry.get("cached_at", 0.0))) >= _ttl_seconds():
            return None
        return entry


def put(sig: str, result: dict) -> None:
    """[FR-04] Persist a fresh cache entry for ``sig`` with current timestamp.

    Adds ``cached_at`` (epoch seconds) and writes the document atomically
    under ``store.STORE_LOCK`` so concurrent writers serialise their
    read-modify-write (NFR-08).
    """
    with store.STORE_LOCK:
        data = _load()
        data.setdefault("entries", {})[sig] = _make_entry(result)
        _save(data)