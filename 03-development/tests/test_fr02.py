"""FR-02: Task execution — TDD-RED failing tests.

Tests in this file exercise `taskq run <id>` and `taskq run --all` per
SRS.md FR-02 and TEST_SPEC.md (rows #1–#7 for FR-02).

Test strategy:
- Tests 1, 2, 4, 7 exercise `executor.run` / `executor.run_all` directly
  (in-process). This is the internal logic per SAD §2.2.3.
- Tests 3 and 6 exercise the CLI surface (`taskq run <id>` and
  `taskq run --all`) so they verify the full dispatch + exit-code path
  (single-task timeout → exit 4, run --all concurrency).
- Tests 3 and 6 ALSO have subprocess variants for end-to-end coverage.
  Per INTEGRATION FR GUIDELINES, pytest-cov cannot measure code running
  in a subprocess; the in-process tests above provide that coverage.
- Test 5 is a static source scan — it imports `taskq.executor` (so the
  Collection Error is RED when the module is missing) and asserts the
  absence of `shell=True` plus the presence of `shlex.split`.

NOTE: These tests are expected to FAIL with a Collection Error
(ModuleNotFoundError: No module named 'taskq.executor') because the
executor module does not exist yet. That is the GREEN step's job. Do
not add try/except ImportError to hide the failure — the Collection
Error is the valid RED state.
"""

from __future__ import annotations

import datetime
import json as json_lib
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

# Standard top-level import — Collection Error (Exit Code 2) is the valid
# RED state when source code does not exist yet.
from taskq import cli, executor  # noqa: E402  (collection error is expected)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

# SRC_ROOT so subprocess tests can propagate PYTHONPATH (pytest's `pythonpath`
# setting does NOT propagate to child processes automatically).
SRC_ROOT = Path(__file__).resolve().parent.parent / "src"


@pytest.fixture
def taskq_home(tmp_path, monkeypatch):
    """Per-test isolated $TASKQ_HOME.

    Sets TASKQ_TASK_TIMEOUT=1 so a `sleep 5` reliably times out for
    test_fr02_03 (test #3). Sets TASKQ_MAX_WORKERS=4 so the fanout
    for tests #6 and #7 is deterministic.
    """
    home = tmp_path / ".taskq"
    home.mkdir()
    monkeypatch.setenv("TASKQ_HOME", str(home))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TASKQ_TASK_TIMEOUT", "1")
    monkeypatch.setenv("TASKQ_MAX_WORKERS", "4")
    return home


def _build_env(taskq_home: Path) -> dict:
    """Build a child-process env that propagates TASKQ_HOME + PYTHONPATH and
    inherits TASKQ_* tunables (set by the parent fixture via monkeypatch).
    """
    env = os.environ.copy()
    env["TASKQ_HOME"] = str(taskq_home)
    env["PYTHONPATH"] = str(SRC_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return env


def _run_cli(args, taskq_home: Path, timeout: int = 30):
    """Run `python -m taskq ...` as a subprocess for the given isolated home."""
    cmd = [sys.executable, "-m", "taskq", *args]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=_build_env(taskq_home),
        timeout=timeout,
    )


def _read_tasks_json(taskq_home: Path) -> dict:
    """Read and parse $TASKQ_HOME/tasks.json; raise if file does not exist."""
    path = taskq_home / "tasks.json"
    if not path.exists():
        raise AssertionError(
            f"Expected tasks.json at {path}, but it was not written."
        )
    return json_lib.loads(path.read_text(encoding="utf-8"))


def _seed_via_cli(taskq_home: Path, command: str, name: str | None = None) -> str:
    """Seed a pending task via the existing `taskq submit` CLI; return id."""
    args = ["submit", command]
    if name is not None:
        args += ["--name", name]
    result = _run_cli(args, taskq_home)
    if result.returncode != 0:
        raise AssertionError(
            f"submit seed failed: exit={result.returncode}; "
            f"stderr={result.stderr!r}"
        )
    return result.stdout.strip()


def _seed_directly(taskq_home: Path, count: int) -> list[str]:
    """Seed `count` pending tasks by writing tasks.json directly.

    Returns the list of generated task ids. Used for fanout-heavy tests
    (concurrency / storage integrity) where per-task subprocess seeding
    would dominate runtime.
    """
    tasks: dict[str, dict] = {}
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    for i in range(count):
        tid = uuid.uuid4().hex[:8]
        tasks[tid] = {
            "status": "pending",
            "command": f"echo task-{i}",
            "created_at": now,
        }
    store = {"version": 1, "tasks": tasks}
    path = taskq_home / "tasks.json"
    path.write_text(
        json_lib.dumps(store, ensure_ascii=False),
        encoding="utf-8",
    )
    return list(tasks.keys())


# ---------------------------------------------------------------------------
# Case #1: test_fr02_01_successful_command_result_fields  (happy path)
# ---------------------------------------------------------------------------

def test_fr02_01_successful_command_result_fields(taskq_home):
    """FR-02: a successful command reaches `done` with exit_code 0 and
    all required result fields are recorded.

    Sub-assertion rule: FR02-AC1-success-command — `len(command) > 0`.

    NFR associations:
    # NFR-04 — secret redaction runs on stdout_tail/stderr_tail BEFORE
    # store.update; here we verify the tails themselves are populated
    # with non-empty content for a non-secret command.
    """
    # GREEN TODO: taskq.executor must have `run(task_id: str) -> int`
    # which executes the task's command via subprocess.run with
    # shell=False, captures stdout/stderr, trims to the last 2000 chars,
    # and records exit_code, stdout_tail, stderr_tail, duration_ms,
    # finished_at into the task record under a shared store.Lock.
    task_id = _seed_via_cli(taskq_home, "echo hi")

    rc = executor.run(task_id)

    assert rc == 0, (
        f"executor.run returned {rc}; expected 0 for a successful command"
    )

    stored = _read_tasks_json(taskq_home)
    record = stored["tasks"][task_id]

    assert record["status"] == "done", (
        f"task status must be 'done' on exit 0; got {record['status']!r}"
    )
    assert record["exit_code"] == 0, (
        f"recorded exit_code must be 0 for 'echo hi'; got {record['exit_code']!r}"
    )
    # Required result fields per SPEC §3 FR-02.
    for field in ("stdout_tail", "stderr_tail", "duration_ms", "finished_at"):
        assert field in record, (
            f"missing required result field {field!r} in record {record!r}"
        )
    # stdout_tail must contain the actual command output ('hi').
    assert "hi" in record["stdout_tail"], (
        f"stdout_tail must reflect 'echo hi' output; got "
        f"{record['stdout_tail']!r}"
    )
    # duration_ms is non-negative; finished_at is an ISO timestamp.
    assert isinstance(record["duration_ms"], int) and record["duration_ms"] >= 0, (
        f"duration_ms must be a non-negative int; got {record['duration_ms']!r}"
    )
    assert isinstance(record["finished_at"], str) and record["finished_at"], (
        f"finished_at must be a non-empty ISO string; got "
        f"{record['finished_at']!r}"
    )


# ---------------------------------------------------------------------------
# Case #2: test_fr02_02_nonzero_failure  (validation / non-zero exit)
# ---------------------------------------------------------------------------

def test_fr02_02_nonzero_failure(taskq_home):
    """FR-02: a non-zero exit command reaches `failed` and the recorded
    exit_code matches the subprocess return value.

    Sub-assertion rule: FR02-AC2-nonzero-command — `len(command) > 0`.

    Note: `executor.run` itself returns 0 (run completed; task is in
    failed state). The CLI exit code is 0 because the run did not
    crash — only the task's exit_code is non-zero. CLI exit 4 is
    reserved for single-task timeout (test #3).
    """
    # GREEN TODO: taskq.executor.run must record `failed` status with
    # the subprocess exit_code when the subprocess returns non-zero.
    task_id = _seed_via_cli(taskq_home, "false")

    rc = executor.run(task_id)

    # The executor returns 0 because the run itself completed; the
    # *task* is in failed state with non-zero exit_code.
    assert rc == 0, (
        f"executor.run returned {rc}; expected 0 (task is failed but run "
        f"completed successfully)"
    )

    stored = _read_tasks_json(taskq_home)
    record = stored["tasks"][task_id]

    assert record["status"] == "failed", (
        f"task status must be 'failed' for non-zero exit; got "
        f"{record['status']!r}"
    )
    assert record["exit_code"] != 0, (
        f"recorded exit_code must be non-zero for 'false'; got "
        f"{record['exit_code']!r}"
    )
    # `false` exits with 1 — be specific.
    assert record["exit_code"] == 1, (
        f"`false` exits 1; recorded exit_code was {record['exit_code']}"
    )
    # stdout_tail and stderr_tail must still be present (NFR-04 boundary).
    assert "stdout_tail" in record, "stdout_tail must be recorded even on failure"
    assert "stderr_tail" in record, "stderr_tail must be recorded even on failure"


# ---------------------------------------------------------------------------
# Case #3: test_fr02_03_timeout_exit_4  (NP-15 timeout → exit 4)
# ---------------------------------------------------------------------------

def test_fr02_03_timeout_exit_4(taskq_home):
    """FR-02: a single-task run that exceeds TASKQ_TASK_TIMEOUT transitions
    to `timeout` and the CLI exits 4.

    Sub-assertion rule: FR02-AC3-timeout-bound — `timeout_seconds == "1"`.
    Active pattern: NP-15 (subprocess timeout).

    NFR associations:
    # NFR-15 — single-task timeout must be mapped, not propagated as
    # an uncaught TimeoutExpired.
    """
    # GREEN TODO: taskq.cli must add a `run` subcommand with a positional
    # `task_id`. cli.run_one must translate a TimeoutExpired sentinel
    # from executor.run into the OS exit code 4 (SPEC §7).
    task_id = _seed_via_cli(taskq_home, "sleep 5")

    # --- In-process: cli.main(["run", task_id]) → exit 4 ---
    exit_code = cli.main(["run", task_id])

    assert exit_code == 4, (
        f"cli.main returned {exit_code}; expected 4 on single-task timeout "
        f"(SPEC §7)"
    )

    stored = _read_tasks_json(taskq_home)
    record = stored["tasks"][task_id]
    assert record["status"] == "timeout", (
        f"task status must be 'timeout' on TimeoutExpired; got "
        f"{record['status']!r}"
    )
    # Timeout is recorded as exit_code 4 (SAD §3.1 / SPEC §7).
    assert record.get("exit_code") == 4, (
        f"timeout exit_code must be 4; got {record.get('exit_code')!r}"
    )

    # --- Subprocess: real CLI on a fresh home for end-to-end check ---
    home2 = taskq_home.parent / ".taskq_fr02_03_subproc"
    home2.mkdir()
    try:
        fresh_id = _seed_directly(home2, 1)[0]
        # Override the seeded command to the long-sleep that will time out.
        store = json_lib.loads((home2 / "tasks.json").read_text(encoding="utf-8"))
        store["tasks"][fresh_id]["command"] = "sleep 5"
        (home2 / "tasks.json").write_text(
            json_lib.dumps(store, ensure_ascii=False),
            encoding="utf-8",
        )
        result = subprocess.run(
            [sys.executable, "-m", "taskq", "run", fresh_id],
            capture_output=True,
            text=True,
            env=_build_env(home2),
            timeout=15,
        )
        assert result.returncode == 4, (
            f"subprocess: expected exit 4 for single-task timeout, got "
            f"{result.returncode}; stderr={result.stderr!r}"
        )
    finally:
        shutil.rmtree(home2, ignore_errors=True)


# ---------------------------------------------------------------------------
# Case #4: test_fr02_04_output_tails_2000_chars  (tail length bound)
# ---------------------------------------------------------------------------

def test_fr02_04_output_tails_2000_chars(taskq_home):
    """FR-02: stdout_tail and stderr_tail each retain at most the last
    2000 characters of subprocess output.

    Sub-assertion rule: FR02-AC4-tail-bound — `tail_bound == "2000"`.

    NFR associations:
    # NFR-04 — secret redaction operates on these tails; the tail-bound
    # is the upstream contract that redaction depends on.
    """
    # GREEN TODO: taskq.executor.run must slice `result.stdout[-2000:]`
    # and `result.stderr[-2000:]` when persisting the result record.
    # 5000 'x' chars + newline = 5001 chars; tail should be exactly 2000.
    task_id = _seed_via_cli(
        taskq_home, 'python -c \'print("x"*5000)\''
    )

    rc = executor.run(task_id)

    assert rc == 0, (
        f"executor.run returned {rc}; expected 0 for a successful "
        f"long-output command"
    )

    stored = _read_tasks_json(taskq_home)
    record = stored["tasks"][task_id]

    assert "stdout_tail" in record, "stdout_tail must be recorded"
    assert "stderr_tail" in record, "stderr_tail must be recorded"

    assert len(record["stdout_tail"]) <= 2000, (
        f"stdout_tail length {len(record['stdout_tail'])} exceeds bound 2000"
    )
    assert len(record["stderr_tail"]) <= 2000, (
        f"stderr_tail length {len(record['stderr_tail'])} exceeds bound 2000"
    )
    # 5000 'x' chars → stdout_tail = exactly 2000 'x' chars (the tail).
    assert record["stdout_tail"] == "x" * 2000, (
        f"stdout_tail should be exactly the last 2000 chars ('x'*2000); "
        f"got len={len(record['stdout_tail'])} value={record['stdout_tail'][:50]!r}..."
    )


# ---------------------------------------------------------------------------
# Case #5: test_fr02_05_no_shell_true_safe_splitting  (static scan)
# ---------------------------------------------------------------------------

def test_fr02_05_no_shell_true_safe_splitting():
    """FR-02: executor must NOT use `shell=True`; it must split commands
    via `shlex.split` so shell metacharacters cannot inject.

    Sub-assertion rule: FR02-AC5-no-shell-true — `forbid_token == "shell=True"`.
    Active pattern: NP-08 (security: no shell=True).

    NFR associations:
    # NFR-02 — security: any `shell=True` call site is a vulnerability;
    # the static scan across src/taskq/ guards against regression.
    """
    # GREEN TODO: taskq.executor.run must call
    # `subprocess.run(shlex.split(command), shell=False,
    #  capture_output=True, text=True, timeout=TASKQ_TASK_TIMEOUT)`
    # for every command. The literal substring "shell=True" must never
    # appear in any file under src/taskq/.
    #
    # Touch `executor` so the import is exercised (raises Collection
    # Error when the module is missing — the valid RED state).
    _ = executor  # noqa: F841  (force import resolution for Collection Error)

    src_pkg = SRC_ROOT / "taskq"
    py_files = list(src_pkg.rglob("*.py"))
    assert py_files, "expected at least one source file under src/taskq"

    shell_true_files: list[str] = []
    shlex_split_files: list[str] = []
    for f in py_files:
        text = f.read_text(encoding="utf-8")
        if "shell=True" in text:
            shell_true_files.append(str(f.relative_to(SRC_ROOT)))
        if "shlex.split" in text:
            shlex_split_files.append(str(f.relative_to(SRC_ROOT)))

    assert not shell_true_files, (
        f"shell=True found in source files: {shell_true_files}; "
        f"FR-02 requires shell=False with shlex.split for every "
        f"subprocess invocation (NFR-02)"
    )
    assert shlex_split_files, (
        "no source file under src/taskq uses shlex.split; FR-02 requires "
        "shlex.split() for safe command parsing (NFR-02)"
    )


# ---------------------------------------------------------------------------
# Case #6: test_fr02_06_run_all_concurrency  (ThreadPoolExecutor fanout)
# ---------------------------------------------------------------------------

def test_fr02_06_run_all_concurrency(taskq_home):
    """FR-02: `taskq run --all` executes all pending tasks concurrently
    via ThreadPoolExecutor(max_workers=TASKQ_MAX_WORKERS) and leaves no
    orphaned pending entries.

    Sub-assertion rule: FR02-AC6-concurrency-fanout — `max_workers < task_count`.

    NFR associations:
    # NFR-08 — concurrency: shared store.Lock serialises writes from
    # the worker pool so tasks.json is never corrupted.
    # NFR-09 — performance: bounded fan-out keeps memory usage and
    # total wall time in check; no task is silently dropped.
    """
    # GREEN TODO: taskq.cli must add a `run` subcommand supporting
    # `--all` (mutually exclusive with the positional task_id).
    # cli.run_all must dispatch to executor.run_all(), which uses
    # ThreadPoolExecutor(max_workers=TASKQ_MAX_WORKERS).
    n_tasks = 8
    max_workers = 4  # matches fixture's TASKQ_MAX_WORKERS
    assert max_workers < n_tasks, (
        "precondition: max_workers must be < task_count for fanout to matter"
    )

    seeded_ids = _seed_directly(taskq_home, n_tasks)
    assert len(seeded_ids) == n_tasks

    # --- In-process: cli.main(["run", "--all"]) → exit 0 or 3 (breaker) ---
    exit_code = cli.main(["run", "--all"])
    assert exit_code in (0, 3), (
        f"cli.run_all returned {exit_code}; expected 0 (success) or 3 "
        f"(breaker OPEN, FR-03 sentinel)"
    )

    stored = _read_tasks_json(taskq_home)
    tasks = stored["tasks"]
    assert len(tasks) == n_tasks, (
        f"expected {n_tasks} tasks after run --all; got {len(tasks)} "
        f"(task loss detected)"
    )
    # Every seeded task must still be in tasks.json (no losses).
    for tid in seeded_ids:
        assert tid in tasks, f"task {tid} lost from storage under concurrency"
        rec = tasks[tid]
        # No orphaned pending — every task transitioned to a terminal state.
        assert rec["status"] in ("done", "failed", "timeout"), (
            f"task {tid} not terminal after run --all: status={rec['status']!r}"
        )

    # --- Subprocess: real CLI on a fresh home for end-to-end check ---
    home2 = taskq_home.parent / ".taskq_fr02_06_subproc"
    home2.mkdir()
    try:
        _seed_directly(home2, n_tasks)
        result = subprocess.run(
            [sys.executable, "-m", "taskq", "run", "--all"],
            capture_output=True,
            text=True,
            env=_build_env(home2),
            timeout=30,
        )
        assert result.returncode in (0, 3), (
            f"subprocess: expected exit 0 or 3, got {result.returncode}; "
            f"stderr={result.stderr!r}"
        )
        # tasks.json must still be valid JSON (atomic write + lock).
        stored2 = json_lib.loads(
            (home2 / "tasks.json").read_text(encoding="utf-8")
        )
        assert len(stored2["tasks"]) == n_tasks, (
            f"subprocess: expected {n_tasks} tasks, got "
            f"{len(stored2['tasks'])}"
        )
    finally:
        shutil.rmtree(home2, ignore_errors=True)


# ---------------------------------------------------------------------------
# Case #7: test_fr02_07_thread_safe_lossless_storage  (concurrent writers)
# ---------------------------------------------------------------------------

def test_fr02_07_thread_safe_lossless_storage(taskq_home):
    """FR-02: concurrent task execution under one ThreadPoolExecutor must
    not corrupt tasks.json — every writer attempt either commits or raises
    under the shared lock; no entry is silently dropped or duplicated.

    Sub-assertion rule: FR02-AC7-writer-count — `writer_count > "1"`.

    NFR associations:
    # NFR-08 — concurrency: shared store.Lock is the single point of
    # serialisation for tasks.json writes across the worker pool.
    # NFR-09 — performance: lossless storage means no task is dropped
    # under concurrent load; all 50 records are accounted for.
    """
    # GREEN TODO: taskq.executor.run_all must serialise task-result
    # updates through a shared `taskq.store` Lock so that concurrent
    # worker writes cannot interleave (which would corrupt tasks.json).
    writer_count = 4  # mirrors TASKQ_MAX_WORKERS fixture
    task_count = 50

    seeded_ids = _seed_directly(taskq_home, task_count)
    assert len(seeded_ids) == task_count

    # Direct call to executor.run_all (in-process) — exercises the
    # worker pool's lock-protected writes without going through cli.
    rc = executor.run_all()
    assert rc in (0, 3), (
        f"executor.run_all returned {rc}; expected 0 or 3 (breaker sentinel)"
    )

    # tasks.json must exist, be valid JSON, contain all seeded ids,
    # and have unique id keys (no duplicates from interleaved writes).
    path = taskq_home / "tasks.json"
    assert path.exists(), "tasks.json must exist after run_all"
    raw = path.read_text(encoding="utf-8")
    # No partial writes — the file must parse cleanly.
    stored = json_lib.loads(raw)
    assert isinstance(stored, dict), (
        f"tasks.json root must be a dict; got {type(stored).__name__}"
    )
    assert "tasks" in stored, "tasks.json must contain a 'tasks' key"
    tasks = stored["tasks"]
    assert isinstance(tasks, dict), (
        f"tasks.json 'tasks' must be a dict; got {type(tasks).__name__}"
    )

    # Lossless: every seeded id is present exactly once.
    assert len(tasks) == task_count, (
        f"expected {task_count} tasks after concurrent run_all, got "
        f"{len(tasks)} (task loss or duplication detected)"
    )
    for tid in seeded_ids:
        assert tid in tasks, (
            f"task {tid} lost under concurrent writers "
            f"(writer_count={writer_count}, task_count={task_count})"
        )
        rec = tasks[tid]
        assert rec["status"] in ("done", "failed", "timeout"), (
            f"task {tid} not terminal after concurrent run_all: "
            f"status={rec['status']!r}"
        )


# ---------------------------------------------------------------------------
# Coverage-gap tests: exercise branches that 100% coverage requires but the
# FR02-AC behavioural cases above do not hit. Each test name mirrors the
# branch it covers (FR02-COV-decode-bytes, FR02-COV-run-unknown-id,
# FR02-COV-run-all-empty) so the coverage report and the gate2 audit can
# trace why every executable statement is exercised.
# ---------------------------------------------------------------------------

def test_fr02_cov_decode_capture_with_bytes(taskq_home):
    """FR-02 coverage gap: ``executor._decode_capture`` must handle bytes
    input from ``subprocess.TimeoutExpired`` whose ``stdout``/``stderr``
    attributes are bytes on POSIX (FR-02 executor). The function decodes
    with ``errors='replace'`` so non-UTF-8 bytes never raise.
    """
    from taskq.executor import _decode_capture
    # Plain ASCII bytes round-trip identically.
    assert _decode_capture(b"hello world") == "hello world"
    # Non-UTF-8 bytes use the replacement-character escape hatch.
    assert _decode_capture(b"\xff\xfe") == "��"
    # str passes through unchanged; None becomes "".
    assert _decode_capture("ok") == "ok"
    assert _decode_capture(None) == ""


def test_fr02_cov_run_returns_zero_for_unknown_id(taskq_home):
    """FR-02 coverage gap: ``executor.run`` returns 0 (no-op) when the
    requested ``task_id`` is not in the store. This branch is a defensive
    guard for the CLI's ``taskq run <id>`` path when an id is mistyped.
    """
    rc = executor.run("deadbeef")
    assert rc == 0, (
        f"executor.run('deadbeef') returned {rc}; expected 0 for unknown id"
    )


def test_fr02_cov_run_all_returns_zero_when_no_pending(taskq_home):
    """FR-02 coverage gap: ``executor.run_all`` returns 0 when the store
    contains zero pending tasks. The CLI's ``taskq run --all`` must
    short-circuit instead of spinning up a worker pool.
    """
    # Empty store — no tasks at all.
    rc = executor.run_all()
    assert rc == 0, (
        f"executor.run_all() on empty store returned {rc}; expected 0"
    )
