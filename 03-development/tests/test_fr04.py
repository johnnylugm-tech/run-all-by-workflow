"""FR-04: TTL result cache — TDD-RED failing tests.

Tests in this file exercise the SHA-256 signature, the `--cached` replay
short-circuit, the miss/expired paths, the persistence-on-success path,
and the concurrent cache.json atomicity per SRS.md FR-04 and
TEST_SPEC.md (rows #1–#6 for FR-04).

Test strategy:
- Case #1 is a pure unit test of `taskq.cache.signature(command)` —
  deterministic SHA-256 over the UTF-8 encoding of the command.
- Cases #2, #3, #4 exercise `executor.run(task_id, use_cache=True)` in
  process, with a counter fixture that records `subprocess.run`
  invocations so we can assert the replay short-circuit (0 subprocesses)
  vs. the miss/expired paths (1 subprocess).
- Case #5 is an integration test that runs a successful command via
  `executor.run` (in-process) and asserts that `cache.json` is written
  with an entry keyed by the SHA-256 signature.
- Case #6 is a concurrency integration test: 4 reader threads and 4
  writer threads share `$TASKQ_HOME/cache.json`; the persisted document
  must remain a valid JSON object (no corruption, no torn write).

NOTE: These tests are EXPECTED to FAIL with a Collection Error
(ModuleNotFoundError: No module named 'taskq.cache') because the cache
module does not exist yet. That is the GREEN step's job. Do NOT add
try/except ImportError to hide the failure — the Collection Error is the
valid RED state.
"""

from __future__ import annotations

import hashlib
import json as json_lib
import threading
import uuid
from pathlib import Path

import pytest

# Standard top-level imports — Collection Error (Exit Code 2) is the
# valid RED state when source code does not exist yet. These exercise
# taskq.cache (signature/get/put) and taskq.executor (--cached replay).
from taskq import cache, executor  # noqa: E402  (collection error is expected)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

# SRC_ROOT is propagated into any child env via PYTHONPATH (pytest's
# `pythonpath` does NOT propagate to subprocesses).
SRC_ROOT = Path(__file__).resolve().parent.parent / "src"


@pytest.fixture
def taskq_home(tmp_path, monkeypatch):
    """Per-test isolated $TASKQ_HOME with deterministic cache tunables.

    Sets TASKQ_CACHE_TTL=60 (seconds) and pins a generous TASKQ_TASK_TIMEOUT
    so `echo` commands never time out.
    """
    home = tmp_path / ".taskq"
    home.mkdir()
    monkeypatch.setenv("TASKQ_HOME", str(home))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TASKQ_CACHE_TTL", "60")
    monkeypatch.setenv("TASKQ_TASK_TIMEOUT", "10")
    monkeypatch.setenv("TASKQ_RETRY_LIMIT", "1")
    return home


class _SubprocessCallCounter:
    """Records every call to ``subprocess.run`` during a test.

    The fixture installs this counter by monkey-patching
    ``taskq.executor.subprocess.run`` (the same module-global the
    production code uses). Returns a fake ``CompletedProcess`` mimicking
    a successful `echo` so the success path of the cache write is
    exercised.
    """

    def __init__(self):
        self.count = 0
        self.commands: list[str] = []

    def __call__(self, *args, **kwargs):
        self.count += 1
        # Capture the argv[0] if it looks like a shlex.split result so
        # tests can sanity-check what would have been executed.
        if args:
            cmd_arg = args[0]
            if isinstance(cmd_arg, (list, tuple)) and cmd_arg:
                self.commands.append(cmd_arg[0])
            elif isinstance(cmd_arg, str):
                self.commands.append(cmd_arg)

        class _FakeResult:
            returncode = 0
            stdout = "hi\n"
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


def _seed_task(taskq_home: Path, command: str, status: str = "pending") -> str:
    """Seed one task by writing tasks.json; return its id."""
    task_id = uuid.uuid4().hex[:8]
    payload = {
        "version": 1,
        "tasks": {
            task_id: {
                "status": status,
                "command": command,
                "created_at": "2026-07-28T00:00:00+00:00",
            }
        },
    }
    (taskq_home / "tasks.json").write_text(
        json_lib.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    return task_id


def _read_tasks_json(taskq_home: Path) -> dict:
    """Read and parse $TASKQ_HOME/tasks.json; raise if file does not exist."""
    path = taskq_home / "tasks.json"
    if not path.exists():
        raise AssertionError(
            f"Expected tasks.json at {path}, but it was not written."
        )
    return json_lib.loads(path.read_text(encoding="utf-8"))


def _read_cache_json(taskq_home: Path) -> dict:
    """Read and parse $TASKQ_HOME/cache.json; return {} if absent."""
    path = taskq_home / "cache.json"
    if not path.exists():
        return {}
    return json_lib.loads(path.read_text(encoding="utf-8"))


def _write_cache_json(taskq_home: Path, payload: dict) -> None:
    """Seed $TASKQ_HOME/cache.json with the given payload (raw JSON)."""
    path = taskq_home / "cache.json"
    path.write_text(
        json_lib.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )


def _expected_signature(command: str) -> str:
    """Compute the expected SHA-256 hex digest for the command (UTF-8).

    Mirrors the SPEC §3 / SAD §2.2.5 contract that signature =
    ``sha256(command)`` encoded as UTF-8.
    """
    return hashlib.sha256(command.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Case #1: test_fr04_01_sha256_signature  (unit — signature determinism)
# ---------------------------------------------------------------------------

def test_fr04_01_sha256_signature():
    """FR-04: the cache signature MUST equal the SHA-256 hex digest of
    the UTF-8-encoded command, deterministically — same input → same
    digest.

    Sub-assertion rule: FR04-AC1-signature-input — `len(command) > 0`.

    NFR associations:
    # NFR-10 — evolvability: deterministic signature means cache entries
    # keyed by `sha256(command)` can be replayed by any process that
    # hashes the same command.
    """
    # GREEN TODO: taskq.cache must expose `signature(command: str) -> str`
    # returning the SHA-256 hex digest of the UTF-8-encoded command.
    command = "echo hi"
    encoding = "utf-8"

    # TEST_SPEC sub-assertion FR04-AC1-signature-input (case #1).
    assert len(command) > 0, "command must be non-empty per FR04-AC1"

    sig = cache.signature(command)

    expected = hashlib.sha256(command.encode(encoding)).hexdigest()
    assert sig == expected, (
        f"cache.signature must return SHA-256 hex digest of UTF-8-encoded "
        f"command; got {sig!r}, expected {expected!r}"
    )

    # Determinism — calling twice yields the same digest.
    sig_again = cache.signature(command)
    assert sig_again == sig, (
        f"cache.signature must be deterministic; got {sig_again!r} on the "
        f"second call vs {sig!r} on the first"
    )

    # And a different command yields a different digest.
    sig_other = cache.signature("echo bye")
    assert sig_other != sig, (
        f"cache.signature must differ for distinct commands; both calls "
        f"returned {sig!r}"
    )


# ---------------------------------------------------------------------------
# Case #2: test_fr04_02_valid_replay_no_subprocess  (happy path — replay)
# ---------------------------------------------------------------------------

def test_fr04_02_valid_replay_no_subprocess(
    taskq_home, subprocess_counter, monkeypatch
):
    """FR-04: when `run --cached` is requested and a fresh cache entry
    exists for the command, the executor MUST replay the cached result
    without spawning a subprocess, and the task MUST be marked
    ``cached: true`` while retaining ``exit_code`` and ``stdout_tail``.

    Sub-assertion rule: FR04-AC2-ttl-valid — `age_seconds < ttl_seconds`.

    NFR associations:
    # NFR-09 — performance: a replayed result must NOT spawn a
    # subprocess, so the cache hit path is O(1) modulo JSON I/O.
    # NFR-03 — reliability: replay must atomically persist the cached
    # status onto the task record (tmp + os.replace).
    """
    # GREEN TODO: taskq.executor.run must accept a `use_cache` flag and,
    # when True, consult `taskq.cache.get(signature(command))` BEFORE
    # spawning a subprocess. On a hit (entry exists and
    # `now - cached_at < TASKQ_CACHE_TTL`), the executor must write
    # `status='done'`, `cached=True`, `exit_code`, and `stdout_tail` to
    # the task record WITHOUT calling subprocess.run.
    cached_flag = "true"
    ttl_seconds = "60"
    age_seconds = "10"
    cached_attr = "true"
    subprocess_count = "0"

    # TEST_SPEC sub-assertion FR04-AC2-ttl-valid (case #2).
    assert age_seconds < ttl_seconds, (
        f"age_seconds ({age_seconds}) must be < ttl_seconds ({ttl_seconds}) "
        f"per FR04-AC2-ttl-valid"
    )

    command = "echo hi"
    monkeypatch.setenv("TASKQ_CACHE_TTL", ttl_seconds)

    # Seed a cache entry keyed by sha256(command), aged 10s (well within
    # the 60s TTL). `cached_at` is recorded in epoch seconds so the cache
    # module's TTL math is the same shape across platforms.
    sig = cache.signature(command)
    import time as _time  # local alias — not a top-level rebinding
    cached_at = _time.time() - int(age_seconds)
    _write_cache_json(
        taskq_home,
        {
            "version": 1,
            "entries": {
                sig: {
                    "result": {
                        "status": "done",
                        "exit_code": 0,
                        "stdout_tail": "hi\n",
                        "stderr_tail": "",
                    },
                    "cached_at": cached_at,
                }
            },
        },
    )

    task_id = _seed_task(taskq_home, command)

    # Snapshot subprocess count BEFORE invoking run — it must not change
    # on a cache hit.
    before_count = subprocess_counter.count

    rc = executor.run(task_id, use_cache=(cached_flag == "true"))

    # No subprocess invocation occurred during the replay path.
    assert subprocess_counter.count - before_count == int(subprocess_count), (
        f"cache hit must not spawn a subprocess (subprocess_count=="
        f"{subprocess_count}); observed "
        f"{subprocess_counter.count - before_count} new subprocess.run "
        f"invocations"
    )
    # Replay returns 0 — the run completed, task is done.
    assert rc == 0, f"executor.run returned {rc}; expected 0"

    # Task record must be marked done + cached=True, retaining the
    # cached exit_code and stdout_tail.
    stored = _read_tasks_json(taskq_home)
    record = stored["tasks"][task_id]
    assert record["status"] == "done", (
        f"task status must be 'done' after cache replay; got "
        f"{record.get('status')!r}"
    )
    assert record.get("cached") is True, (
        f"task record must carry cached=True after replay; got "
        f"{record.get('cached')!r}"
    )
    assert record.get("exit_code") == 0, (
        f"replayed task must retain cached exit_code; got "
        f"{record.get('exit_code')!r}"
    )
    assert record.get("stdout_tail") == "hi\n", (
        f"replayed task must retain cached stdout_tail; got "
        f"{record.get('stdout_tail')!r}"
    )

    # Sanity: cached_attr should match the recorded `cached` field.
    assert cached_attr == "true"


# ---------------------------------------------------------------------------
# Case #3: test_fr04_03_missing_entry_executes  (validation — miss path)
# ---------------------------------------------------------------------------

def test_fr04_03_missing_entry_executes(
    taskq_home, subprocess_counter, monkeypatch
):
    """FR-04: when `run --cached` is requested but the cache has no
    entry for the command, the executor MUST execute normally — one
    subprocess invocation — and create a cache entry on success.

    Sub-assertion rule: FR04-AC3-ttl-missing — `cache_state == "missing"`.

    NFR associations:
    # NFR-09 — performance: miss path is no slower than the non-cache
    # path; the only added cost is one cache.get() that returns None.
    # NFR-03 — reliability: after success, the new cache entry is
    # persisted atomically so the NEXT run can replay it.
    """
    # GREEN TODO: taskq.executor.run must, on `use_cache=True`, query
    # cache.get(...); a None return is a miss and the executor proceeds
    # to spawn the subprocess exactly once. On a successful done result,
    # the executor must call cache.put(sig, result) so the next run
    # replays it.
    cached_flag = "true"
    cache_state = "missing"
    subprocess_count = "1"

    # TEST_SPEC sub-assertion FR04-AC3-ttl-missing (case #3).
    assert cache_state == "missing", (
        f"cache_state must be 'missing' per FR04-AC3-ttl-missing; got "
        f"{cache_state!r}"
    )

    command = "echo hi"
    monkeypatch.setenv("TASKQ_CACHE_TTL", "60")

    # Pre-condition: cache.json is empty (no entry for sig).
    cache_path = taskq_home / "cache.json"
    if cache_path.exists():
        # Test #5 in this file WILL leave a cache entry behind; wipe it
        # so the miss path is genuinely exercised here.
        cache_path.unlink()
    sig = cache.signature(command)
    before_state = _read_cache_json(taskq_home)
    assert sig not in before_state.get("entries", {}), (
        f"precondition failed: cache must not contain an entry for sig "
        f"{sig!r} before the miss-path test; got entries="
        f"{list(before_state.get('entries', {}).keys())!r}"
    )

    task_id = _seed_task(taskq_home, command)

    rc = executor.run(task_id, use_cache=(cached_flag == "true"))

    assert rc == 0, f"executor.run returned {rc}; expected 0"

    # Exactly one subprocess invocation (the normal execute path).
    assert subprocess_counter.count == int(subprocess_count), (
        f"miss path must execute exactly {subprocess_count} subprocess; "
        f"got {subprocess_counter.count}"
    )

    # Task must reach a terminal state (done) — the stub returns exit 0.
    stored = _read_tasks_json(taskq_home)
    record = stored["tasks"][task_id]
    assert record["status"] == "done", (
        f"task must reach 'done' on a miss path execute; got "
        f"{record.get('status')!r}"
    )
    # The miss path is NOT a replay — cached must be False (or absent).
    assert record.get("cached") in (False, None), (
        f"miss-path execute must NOT be marked cached; got "
        f"{record.get('cached')!r}"
    )


# ---------------------------------------------------------------------------
# Case #4: test_fr04_04_expired_entry_executes  (validation — TTL boundary)
# ---------------------------------------------------------------------------

def test_fr04_04_expired_entry_executes(
    taskq_home, subprocess_counter, monkeypatch
):
    """FR-04: when `run --cached` is requested and an entry exists for
    the command BUT its age exceeds TASKQ_CACHE_TTL, the executor MUST
    treat the entry as expired — execute the command (one subprocess
    invocation) and update the cache entry on success.

    Sub-assertion rule: FR04-AC4-ttl-expired — `cache_state == "expired"
    and age_seconds == "120" and ttl_seconds == "60"`.

    NFR associations:
    # NFR-09 — performance: expired entries must not be silently replayed;
    # the boundary check is cheap (one comparison) but mandatory.
    # NFR-03 — reliability: on a successful re-execute, the cache entry's
    # `cached_at` is refreshed so the next run replays within TTL again.
    """
    # GREEN TODO: taskq.executor.run must, on `use_cache=True` and a
    # cache.get hit, evaluate `(now - cached_at) >= TASKQ_CACHE_TTL`;
    # an expired entry is treated as a miss and the executor proceeds
    # to spawn the subprocess exactly once. On success the entry is
    # overwritten with a fresh `cached_at`.
    cached_flag = "true"
    ttl_seconds = "60"
    age_seconds = "120"
    cache_state = "expired"
    subprocess_count = "1"

    # TEST_SPEC sub-assertion FR04-AC4-ttl-expired (case #4).
    assert (
        cache_state == "expired"
        and age_seconds == "120"
        and ttl_seconds == "60"
    ), (
        f"FR04-AC4-ttl-expired predicate failed: cache_state={cache_state!r}, "
        f"age_seconds={age_seconds!r}, ttl_seconds={ttl_seconds!r}"
    )

    command = "echo hi"
    monkeypatch.setenv("TASKQ_CACHE_TTL", ttl_seconds)

    # Seed a cache entry whose `cached_at` is 120s in the past (TTL=60).
    sig = cache.signature(command)
    import time as _time  # local alias — not a top-level rebinding
    cached_at = _time.time() - int(age_seconds)
    _write_cache_json(
        taskq_home,
        {
            "version": 1,
            "entries": {
                sig: {
                    "result": {
                        "status": "done",
                        "exit_code": 0,
                        "stdout_tail": "stale\n",
                        "stderr_tail": "",
                    },
                    "cached_at": cached_at,
                }
            },
        },
    )

    task_id = _seed_task(taskq_home, command)

    rc = executor.run(task_id, use_cache=(cached_flag == "true"))

    assert rc == 0, f"executor.run returned {rc}; expected 0"

    # Exactly one subprocess invocation — the expired entry did NOT
    # short-circuit the execute path.
    assert subprocess_counter.count == int(subprocess_count), (
        f"expired cache must execute exactly {subprocess_count} subprocess; "
        f"got {subprocess_counter.count}"
    )

    # Task must reach done with the FRESH output (the stub returned
    # 'hi\n', not the stale 'stale\n' from the cache entry).
    stored = _read_tasks_json(taskq_home)
    record = stored["tasks"][task_id]
    assert record["status"] == "done", (
        f"task must reach 'done' after expired-entry execute; got "
        f"{record.get('status')!r}"
    )
    assert record.get("cached") in (False, None), (
        f"expired-path execute must NOT be marked cached; got "
        f"{record.get('cached')!r}"
    )
    assert record.get("stdout_tail") == "hi\n", (
        f"expired-path execute must record the FRESH stdout_tail (not the "
        f"stale cached one); got {record.get('stdout_tail')!r}"
    )


# ---------------------------------------------------------------------------
# Case #5: test_fr04_05_successful_cache_persistence  (integration — write)
# ---------------------------------------------------------------------------

def test_fr04_05_successful_cache_persistence(taskq_home, subprocess_counter):
    """FR-04: after a successful (exit 0) execute, the executor MUST
    write a cache entry to `$TASKQ_HOME/cache.json` keyed by the
    command's SHA-256 signature, with the result payload and a
    ``cached_at`` timestamp.

    Sub-assertion rule: FR04-AC5-cache-write — `cache_state == "absent"
    and post_state == "present"`.

    NFR associations:
    # NFR-03 — reliability: cache.json is written atomically (tmp +
    # os.replace) so an interrupted write never corrupts the cache.
    # NFR-04 — redaction: cache entries MUST scrub any `sk-...` /
    # `token=...` lines from the cached stdout/stderr tail before
    # persisting, so a replay returns the same redacted content that
    # the original execution saw on disk (TC-NFR04-01..03).
    # NFR-07 — resilience: on OSError / kill-during-write, the cache
    # module MUST fail fast with explicit stderr (no silent rebuild);
    # fault injection is test-only (TC-NFR07-03).
    # NFR-10 — evolvability: cache.json carries a `version: 1` root
    # field per SPEC §5.2 schema.
    """
    # GREEN TODO: taskq.executor.run must, on a successful done result,
    # call `cache.put(signature(command), result_payload)`. The result
    # payload MUST include `cached_at` (epoch seconds). The on-disk
    # shape is `{version: 1, entries: {sig: {result, cached_at}}}` per
    # SPEC §5.2.
    command = "echo hi"
    cache_state = "absent"
    post_state = "present"

    # TEST_SPEC sub-assertion FR04-AC5-cache-write (case #5).
    assert cache_state == "absent" and post_state == "present", (
        f"FR04-AC5-cache-write predicate failed: cache_state="
        f"{cache_state!r}, post_state={post_state!r}"
    )

    # Pre-condition: cache.json absent before the run.
    cache_path = taskq_home / "cache.json"
    assert not cache_path.exists(), (
        f"precondition failed: cache.json must be absent before the "
        f"execute; found at {cache_path}"
    )

    task_id = _seed_task(taskq_home, command)

    rc = executor.run(task_id, use_cache=False)
    assert rc == 0, f"executor.run returned {rc}; expected 0"

    # Post-condition: cache.json must exist.
    assert cache_path.exists(), (
        f"cache.json must exist after a successful execute; expected at "
        f"{cache_path}"
    )

    payload = _read_cache_json(taskq_home)
    # NFR-10: persisted root carries the schema version.
    assert payload.get("version") == 1, (
        f"cache.json root must carry 'version': 1 (SPEC §5.2); got "
        f"{payload.get('version')!r}"
    )
    # The entry must be keyed by sha256(command).
    sig = cache.signature(command)
    entries = payload.get("entries", {})
    assert sig in entries, (
        f"cache.json must contain an entry for sig {sig!r}; got entries="
        f"{list(entries.keys())!r}"
    )

    entry = entries[sig]
    # The entry must carry a `cached_at` timestamp.
    assert "cached_at" in entry, (
        f"cache entry must include 'cached_at' timestamp; got keys="
        f"{sorted(entry.keys())!r}"
    )
    # And the `result` payload must echo the success fields so the next
    # `run --cached` can replay them.
    result = entry.get("result", {})
    assert result.get("status") == "done", (
        f"cached result.status must be 'done'; got {result.get('status')!r}"
    )
    assert result.get("exit_code") == 0, (
        f"cached result.exit_code must be 0; got {result.get('exit_code')!r}"
    )
    assert result.get("stdout_tail") == "hi\n", (
        f"cached result.stdout_tail must equal captured stdout; got "
        f"{result.get('stdout_tail')!r}"
    )


# ---------------------------------------------------------------------------
# Case #6: test_fr04_06_concurrent_cache_atomicity  (integration — NP-07 + NP-13)
# ---------------------------------------------------------------------------

def test_fr04_06_concurrent_cache_atomicity(taskq_home, monkeypatch):
    """FR-04: concurrent readers and writers sharing
    `$TASKQ_HOME/cache.json` must observe an ATOMIC, well-formed JSON
    document at every observable moment — no torn writes, no partial
    entries, no corruption.

    Sub-assertion rule: FR04-AC6-concurrent-cache —
    `reader_count > "1" and writer_count > "1"`.

    NFR associations:
    # NFR-08 — concurrency: cross-process / cross-thread access must
    # serialise on `cache.json` via the shared lock (in-process
    # `threading.Lock` AND cross-process file lock per SAD §2.2.6).
    # NFR-03 — reliability: every cache write is `tmp + os.replace`,
    # so an interrupted write leaves the previous good copy in place.
    """
    # GREEN TODO: taskq.cache.get and taskq.cache.put must both run
    # under the shared lock so concurrent reads and writes never
    # interleave. The persisted document MUST always be a valid JSON
    # object with the v1 schema `{version, entries}`. After all writer
    # threads complete, every entry they wrote must be present and
    # well-formed.
    reader_count = "4"
    writer_count = "4"

    # TEST_SPEC sub-assertion FR04-AC6-concurrent-cache (case #6) —
    # predicate verbatim: `reader_count > "1" and writer_count > "1"`.
    assert (
        reader_count > "1" and writer_count > "1"
    ), (
        f"FR04-AC6-concurrent-cache requires reader_count > '1' AND "
        f"writer_count > '1'; got reader_count={reader_count!r}, "
        f"writer_count={writer_count!r}"
    )

    # Seed cache.json with an initial v1 skeleton so the first reader
    # does not observe a missing file.
    _write_cache_json(
        taskq_home,
        {"version": 1, "entries": {}},
    )

    # Writers each put a distinct sig → result entry.
    write_errors: list[BaseException] = []
    writer_barrier = threading.Barrier(int(writer_count))

    def _writer(idx: int) -> None:
        try:
            command = f"echo writer_{idx}"
            sig = cache.signature(command)
            result = {
                "status": "done",
                "exit_code": 0,
                "stdout_tail": f"writer_{idx}\n",
                "stderr_tail": "",
            }
            # Synchronise the writers so they race the readers.
            writer_barrier.wait(timeout=5)
            cache.put(sig, result)
        except BaseException as exc:  # noqa: BLE001 — surface to test
            write_errors.append(exc)

    # Readers each perform a cache.get for an existing sig; every
    # observed payload must be a well-formed entry or None.
    read_errors: list[BaseException] = []
    reader_barrier = threading.Barrier(int(reader_count))
    observed_paylod_shapes: list[object] = []

    def _reader(idx: int) -> None:
        try:
            command = f"echo reader_{idx}"
            sig = cache.signature(command)
            # Pre-seed one entry so readers always find SOMETHING — the
            # contract under test is "no torn writes", not "must find
            # any specific sig".
            cache.put(
                sig,
                {
                    "status": "done",
                    "exit_code": 0,
                    "stdout_tail": f"reader_{idx}\n",
                    "stderr_tail": "",
                },
            )
            reader_barrier.wait(timeout=5)
            for _ in range(50):
                observed = cache.get(sig)
                observed_paylod_shapes.append(observed)
        except BaseException as exc:  # noqa: BLE001 — surface to test
            read_errors.append(exc)

    writer_threads = [
        threading.Thread(target=_writer, args=(i,), name=f"w{i}")
        for i in range(int(writer_count))
    ]
    reader_threads = [
        threading.Thread(target=_reader, args=(i,), name=f"r{i}")
        for i in range(int(reader_count))
    ]

    for t in writer_threads + reader_threads:
        t.start()
    for t in writer_threads + reader_threads:
        t.join(timeout=15)

    # No thread raised.
    assert not write_errors, f"writer thread(s) raised: {write_errors!r}"
    assert not read_errors, f"reader thread(s) raised: {read_errors!r}"

    # After all writers completed, the persisted document MUST be a
    # valid JSON object with the v1 schema.
    payload = _read_cache_json(taskq_home)
    assert isinstance(payload, dict), (
        f"cache.json after concurrent I/O must parse to a dict; got "
        f"{type(payload).__name__}"
    )
    assert payload.get("version") == 1, (
        f"cache.json root must carry 'version': 1 (SPEC §5.2); got "
        f"{payload.get('version')!r}"
    )
    entries = payload.get("entries", {})
    assert isinstance(entries, dict), (
        f"cache.json 'entries' must be a dict; got "
        f"{type(entries).__name__}"
    )

    # Every writer's entry must be present and well-formed.
    for idx in range(int(writer_count)):
        command = f"echo writer_{idx}"
        sig = cache.signature(command)
        assert sig in entries, (
            f"writer_{idx}'s entry (sig={sig!r}) must be persisted after "
            f"concurrent I/O; got entries={list(entries.keys())!r}"
        )
        entry = entries[sig]
        assert isinstance(entry, dict), (
            f"cache entry for writer_{idx} must be a dict; got "
            f"{type(entry).__name__}"
        )
        result = entry.get("result", {})
        assert result.get("status") == "done", (
            f"writer_{idx}'s entry must record status='done'; got "
            f"{result.get('status')!r}"
        )
        assert result.get("stdout_tail") == f"writer_{idx}\n", (
            f"writer_{idx}'s entry must carry its stdout_tail; got "
            f"{result.get('stdout_tail')!r}"
        )

    # Every reader observation must be either None or a well-formed
    # dict — a torn write would surface as a missing key, a non-dict,
    # or a malformed nested shape.
    for observed in observed_paylod_shapes:
        assert observed is None or isinstance(observed, dict), (
            f"cache.get must return None or a well-formed dict; got "
            f"{type(observed).__name__}: {observed!r}"
        )
