"""[FR-03] taskq circuit breaker — retry policy + CLOSED/OPEN/HALF_OPEN.

Global, cross-task and cross-process breaker state persisted atomically to
``$TASKQ_HOME/breaker.json`` (SPEC §5.2 ``{version, state, failure_count,
opened_at}``). Also owns the retry/backoff policy that ``taskq.executor``
applies around a failing attempt (SAD §2.2.4).

Citations:
  03-development/tests/test_fr03.py:196 — retry attempts are capped by
    ``TASKQ_RETRY_LIMIT``.
  03-development/tests/test_fr03.py:262 — the delay before retry ``n`` is
    ``TASKQ_BACKOFF_BASE × 2^n``.
  03-development/tests/test_fr03.py:317 — ``TASKQ_BREAKER_THRESHOLD``
    consecutive failures transition the breaker to OPEN and persist it.
  03-development/tests/test_fr03.py:395 — an OPEN breaker rejects a run
    without spawning a subprocess.
  03-development/tests/test_fr03.py:460 — a HALF_OPEN success closes the
    breaker and resets ``failure_count`` to 0.
  03-development/tests/test_fr03.py:520 — a HALF_OPEN failure re-opens the
    breaker and restarts the cooldown clock.
  03-development/tests/test_fr03.py:570 — state written by one process is
    observed by another sharing ``$TASKQ_HOME`` (NFR-08).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from taskq import store

# --- Public constants ---------------------------------------------------

BREAKER_FILENAME = "breaker.json"

CLOSED = "CLOSED"
OPEN = "OPEN"
HALF_OPEN = "HALF_OPEN"

EXIT_BREAKER_OPEN = 3

DEFAULT_RETRY_LIMIT = 2
DEFAULT_BACKOFF_BASE = 0.1
DEFAULT_THRESHOLD = 3
DEFAULT_COOLDOWN = 5.0


# --- Configuration (NFR-06: every knob is an env var with a default) ----

def retry_limit() -> int:
    """[FR-03] Total attempts allowed for one task (``TASKQ_RETRY_LIMIT``)."""
    return int(os.environ.get("TASKQ_RETRY_LIMIT", str(DEFAULT_RETRY_LIMIT)))


def backoff_delay(retry_index: int) -> float:
    """[FR-03] Delay before retry ``retry_index``: ``BACKOFF_BASE × 2^n``."""
    base = float(os.environ.get("TASKQ_BACKOFF_BASE", str(DEFAULT_BACKOFF_BASE)))
    return base * (2 ** retry_index)


def _threshold() -> int:
    """[FR-03] Consecutive failures that open the breaker."""
    return int(os.environ.get("TASKQ_BREAKER_THRESHOLD", str(DEFAULT_THRESHOLD)))


def _cooldown() -> float:
    """[FR-03] Seconds an OPEN breaker waits before allowing a probe."""
    return float(os.environ.get("TASKQ_BREAKER_COOLDOWN", str(DEFAULT_COOLDOWN)))


# --- State machine ------------------------------------------------------

class CircuitBreaker:
    """[FR-03] Persistent CLOSED → OPEN → HALF_OPEN → CLOSED state machine.

    Every transition is a lock-protected read-modify-write followed by an
    atomic ``breaker.json`` write (NFR-03), so a second process sharing
    ``$TASKQ_HOME`` always reads a complete state document (NFR-08).
    """

    def __init__(self, home: Path | None = None):
        """[FR-03] Bind the breaker to ``home`` (defaults to ``TASKQ_HOME``)."""
        self._home = home if home is not None else store.home()

    @property
    def path(self) -> Path:
        """[FR-03] Location of the persisted breaker state document."""
        return self._home / BREAKER_FILENAME

    def state(self) -> str:
        """[FR-03] Current state — OPEN decays to HALF_OPEN after cooldown.

        The decay is computed on read (never written) so any process
        observing an expired OPEN sees HALF_OPEN without a write race.
        """
        data = self._load()
        current = data.get("state", CLOSED)
        if current == OPEN:
            elapsed = time.time() - float(data.get("opened_at", 0.0))
            if elapsed >= _cooldown():
                return HALF_OPEN
        return current

    def allow(self) -> bool:
        """[FR-03] False only while the breaker is OPEN (rejects the run)."""
        return self.state() != OPEN

    def record_failure(self) -> None:
        """[FR-03] Count a final failure; open the breaker when warranted.

        A failure in HALF_OPEN (the trial probe) re-opens immediately and
        restarts the cooldown clock; otherwise the breaker opens once
        ``failure_count`` reaches ``TASKQ_BREAKER_THRESHOLD``.
        """
        with store.STORE_LOCK:
            data = self._load()
            was_half_open = self.state() == HALF_OPEN
            count = int(data.get("failure_count", 0)) + 1
            data["failure_count"] = count
            if was_half_open or count >= _threshold():
                data["state"] = OPEN
                data["opened_at"] = time.time()
            self._save(data)

    def record_success(self) -> None:
        """[FR-03] Close the breaker and clear the failure history."""
        with store.STORE_LOCK:
            self._save({
                "version": 1,
                "state": CLOSED,
                "failure_count": 0,
                "opened_at": 0.0,
            })

    # --- Persistence ----------------------------------------------------

    def _load(self) -> dict:
        """[FR-03] Read breaker.json; fail fast on a corrupt document.

        A missing file is the legitimate cold-start case and yields the v1
        skeleton. A file that exists but does not parse is NOT silently
        rebuilt — it is surfaced as an error naming the path (NFR-07).
        """
        if not self.path.exists():
            return {
                "version": 1,
                "state": CLOSED,
                "failure_count": 0,
                "opened_at": 0.0,
            }
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"corrupt breaker state file: {self.path}") from exc

    def _save(self, data: dict) -> None:
        """[FR-03] Atomically persist the state document (NFR-03)."""
        data["version"] = 1
        store._atomic_write_json(self.path, data)
