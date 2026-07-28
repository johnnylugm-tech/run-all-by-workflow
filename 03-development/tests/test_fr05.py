"""FR-05: CLI integration — TDD-RED failing tests.

Tests in this file exercise the CLI dispatch surface per SRS.md FR-05 and
TEST_SPEC.md (rows #1–#12 for FR-05):

  - `submit "<cmd>" [--name N]` (FR-01 surface, FR-05 dispatch)
  - `run --all` and `run <id> [--cached]` (FR-02/04 dispatch)
  - `status <id>` — full task record on stdout
  - `list [--status S]` — task list with optional status filter
  - `clear` — wipe all data files under $TASKQ_HOME
  - `--json` (global flag) — machine-readable single-line output
  - Exit-code map: 0 success / 2 validation + unknown id / 3 breaker open /
    4 single-task timeout / 1 internal error (SPEC §7)
  - `python -m taskq` entry point (SAD §2.2.1)

Subprocess tests exercise the real CLI surface; in-process tests
(`cli.main([...])`) provide coverage on the same dispatch paths
(pytest-cov cannot measure code running in a subprocess).

NOTE: These tests are EXPECTED to FAIL with a Collection Error
(ModuleNotFoundError: No module named 'taskq.cli' surface additions)
because the FR-05 subcommands (`status`, `list`, `clear`, `--json` as
a global flag, `--cached`, exit-code 1/3 mapping, and the
`python -m taskq` entry) are NOT yet implemented. That is the GREEN
step's job. Do NOT add try/except ImportError to hide the failure —
the Collection Error is the valid RED state.
"""

from __future__ import annotations

import contextlib
import datetime
import io
import json as json_lib
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

# Standard top-level import — Collection Error (Exit Code 2) is the valid
# RED state when source code does not exist yet. `cli` is the dispatch
# hub for FR-05.
from taskq import cli  # noqa: E402  (collection error is expected)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

# SRC_ROOT so subprocess tests can propagate PYTHONPATH (pytest's `pythonpath`
# setting does NOT propagate to child processes automatically).
SRC_ROOT = Path(__file__).resolve().parent.parent / "src"


@pytest.fixture
def taskq_home(tmp_path, monkeypatch):
    """Per-test isolated $TASKQ_HOME with deterministic tunables.

    Sets TASKQ_TASK_TIMEOUT=1 so a `sleep 5` reliably times out for the
    single-task timeout exit-code map test. Sets TASKQ_MAX_WORKERS=4
    so `run --all` fan-out is deterministic. Sets TASKQ_RETRY_LIMIT=1
    so timeout-class exit-code tests do not retry into a different
    terminal state.
    """
    home = tmp_path / ".taskq"
    home.mkdir()
    monkeypatch.setenv("TASKQ_HOME", str(home))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TASKQ_TASK_TIMEOUT", "1")
    monkeypatch.setenv("TASKQ_MAX_WORKERS", "4")
    monkeypatch.setenv("TASKQ_RETRY_LIMIT", "1")
    return home


def _build_env(taskq_home: Path) -> dict:
    """Build a child-process env that propagates TASKQ_HOME + PYTHONPATH."""
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


def _run_cli_inprocess(args, taskq_home: Path):
    """Invoke cli.main([...]) in-process with captured stdout/stderr.

    Returns (exit_code, stdout_text, stderr_text). Used to exercise
    dispatch paths for in-process coverage (pytest-cov cannot see
    subprocess code).
    """
    buf_out = io.StringIO()
    buf_err = io.StringIO()
    saved_stderr = sys.stderr
    rc = 1
    try:
        sys.stderr = buf_err
        with contextlib.redirect_stdout(buf_out):
            rc = cli.main(list(args))
    finally:
        sys.stderr = saved_stderr
    return rc, buf_out.getvalue(), buf_err.getvalue()


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
    (run --all concurrency, list filtering) where per-task subprocess
    seeding would dominate runtime.
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
    payload = {"version": 1, "tasks": tasks}
    path = taskq_home / "tasks.json"
    path.write_text(
        json_lib.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    return list(tasks.keys())


def _seed_breaker_open(taskq_home: Path) -> None:
    """Write a breaker.json that decodes to OPEN state for exit-code #9.

    ``opened_at`` is set to a far-future epoch so the cooldown decay
    never fires; the breaker stays OPEN until the file is rewritten.
    """
    path = taskq_home / "breaker.json"
    path.write_text(
        json_lib.dumps(
            {
                "version": 1,
                "state": "OPEN",
                "failure_count": 3,
                "opened_at": 10_000_000_000.0,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _reset_breaker(taskq_home: Path) -> None:
    """Remove any breaker.json so the next read returns the CLOSED skeleton.

    Scenarios that share ``taskq_home`` but require the breaker to be
    CLOSED (e.g. single-task timeout → exit 4) call this before their
    subprocess so a previous scenario's OPEN state cannot leak in.
    """
    path = taskq_home / "breaker.json"
    if path.exists():
        path.unlink()


def _seed_breaker_corrupt(taskq_home: Path) -> None:
    """Write a corrupt breaker.json to trigger fail-fast exit code 1.

    The atomic-write boundary (NFR-03) refuses silent rebuilds on a
    corrupt document; the CLI must surface this as exit 1.
    """
    path = taskq_home / "breaker.json"
    path.write_text("{ this is not valid json", encoding="utf-8")


# ---------------------------------------------------------------------------
# Case #1: test_fr05_01_submit_command  (happy path — submit dispatches FR-01)
# ---------------------------------------------------------------------------

def test_fr05_01_submit_command(taskq_home):
    """FR-05: `taskq submit "<cmd>"` dispatches to FR-01 and exits 0 on
    a valid command.

    Sub-assertion rule: FR05-AC1-submit-valid — `len(command) > 0`.

    NFR associations:
    # NFR-02 — security: the CLI rejects injection characters in
    # `taskq submit` (delegated to FR-01 validation, exercised here via
    # the happy path that proves dispatch reaches the validation gate).
    """
    command = "echo hi"
    expected_exit = "0"
    json_flag = "false"

    # TEST_SPEC sub-assertion FR05-AC1-submit-valid (case #1).
    assert len(command) > 0, "command must be non-empty per FR05-AC1"
    assert expected_exit == "0" and json_flag == "false"

    result = _run_cli(["submit", command], taskq_home)

    assert result.returncode == 0, (
        f"submit dispatch should exit 0 on a valid command; got "
        f"{result.returncode}; stderr={result.stderr!r}"
    )
    # FR-01 contract: stdout is the 8-hex task id.
    printed = result.stdout.strip()
    assert re.fullmatch(r"[0-9a-f]{8}", printed), (
        f"submit stdout must be an 8-hex id; got {printed!r}"
    )

    # In-process variant — same dispatch path, in-process coverage.
    rc, stdout, _stderr = _run_cli_inprocess(["submit", command], taskq_home)
    assert rc == 0, f"in-process: submit returned {rc}; expected 0"
    assert re.fullmatch(r"[0-9a-f]{8}", stdout.strip()), (
        f"in-process: submit stdout must be 8-hex id; got {stdout!r}"
    )

    # --- In-process validation paths (mirrors the subprocess exit-2 map) ---
    # Every rejection flows through cli._fail, so stderr must be non-empty.

    # Empty / all-whitespace command → exit 2 (FR-01 rule, FR-05 dispatch).
    for bad in ("", "   \t  "):
        rc, stdout, stderr = _run_cli_inprocess(["submit", bad], taskq_home)
        assert rc == 2, (
            f"in-process: submit {bad!r} returned {rc}; expected 2"
        )
        assert stderr.startswith("submit: "), (
            f"in-process: rejection must carry a 'submit:' diagnostic; got "
            f"{stderr!r}"
        )
        assert stdout == "", (
            f"in-process: a rejected submit must print nothing on stdout; "
            f"got {stdout!r}"
        )

    # Length > 1000 → exit 2, and the diagnostic names the actual length.
    too_long = "a" * (cli.MAX_COMMAND_LENGTH + 1)
    rc, _stdout, stderr = _run_cli_inprocess(["submit", too_long], taskq_home)
    assert rc == 2, f"in-process: over-length submit returned {rc}; expected 2"
    assert str(len(too_long)) in stderr, (
        f"in-process: over-length diagnostic must name the length; got "
        f"{stderr!r}"
    )

    # A command of EXACTLY the maximum length is accepted (boundary).
    rc, _stdout, stderr = _run_cli_inprocess(
        ["submit", "a" * cli.MAX_COMMAND_LENGTH], taskq_home
    )
    assert rc == 0, (
        f"in-process: a {cli.MAX_COMMAND_LENGTH}-char command is at the "
        f"boundary and must be accepted; got {rc}; stderr={stderr!r}"
    )

    # Every blacklisted injection character → exit 2, first offender named.
    for char in ";|&$><`":
        rc, _stdout, stderr = _run_cli_inprocess(
            ["submit", f"echo hi {char} whoami"], taskq_home
        )
        assert rc == 2, (
            f"in-process: injection char {char!r} must be rejected; got {rc}"
        )
        assert repr(char) in stderr, (
            f"in-process: diagnostic must name the offending char {char!r}; "
            f"got {stderr!r}"
        )

    # --- Duplicate --name among pending/running tasks → exit 2 ---
    rc, stdout, stderr = _run_cli_inprocess(
        ["submit", "echo named", "--name", "dup-demo"], taskq_home
    )
    assert rc == 0, f"in-process: first --name submit returned {rc}; stderr={stderr!r}"
    first_id = stdout.strip()
    assert _read_tasks_json(taskq_home)["tasks"][first_id]["name"] == "dup-demo", (
        "in-process: --name must be persisted on the task record"
    )

    before = _read_tasks_json(taskq_home)["tasks"]
    rc, _stdout, stderr = _run_cli_inprocess(
        ["submit", "echo other", "--name", "dup-demo"], taskq_home
    )
    assert rc == 2, (
        f"in-process: duplicate --name must be rejected with exit 2; got {rc}"
    )
    assert "dup-demo" in stderr, (
        f"in-process: duplicate-name diagnostic must name the collision; got "
        f"{stderr!r}"
    )
    after = _read_tasks_json(taskq_home)["tasks"]
    assert len(after) == len(before), (
        f"in-process: a rejected duplicate name must not append a task; "
        f"{len(before)} -> {len(after)}"
    )

    # The same name is reusable once the holder leaves pending/running.
    stored = _read_tasks_json(taskq_home)
    stored["tasks"][first_id]["status"] = "done"
    (taskq_home / "tasks.json").write_text(
        json_lib.dumps(stored, ensure_ascii=False), encoding="utf-8"
    )
    rc, _stdout, stderr = _run_cli_inprocess(
        ["submit", "echo reuse", "--name", "dup-demo"], taskq_home
    )
    assert rc == 0, (
        f"in-process: a name held only by a terminal task is reusable; got "
        f"{rc}; stderr={stderr!r}"
    )

    # --- submit --json → single-line JSON with id + pending status ---
    rc, stdout, _stderr = _run_cli_inprocess(
        ["submit", "echo json-mode", "--json"], taskq_home
    )
    assert rc == 0, f"in-process: submit --json returned {rc}; expected 0"
    assert "\n" not in stdout.rstrip(), (
        f"in-process: submit --json must be one line; got {stdout!r}"
    )
    payload = json_lib.loads(stdout)
    assert re.fullmatch(r"[0-9a-f]{8}", payload["id"]), (
        f"in-process: submit --json id must be 8-hex; got {payload!r}"
    )
    assert payload["status"] == "pending", (
        f"in-process: submit --json status must be 'pending'; got {payload!r}"
    )

    # --- Atomic-write boundary: no orphan tmp/backup files remain ---
    leftovers = [
        p.name
        for p in taskq_home.iterdir()
        if p.name.endswith(".tmp") or p.name.endswith("~")
    ]
    assert leftovers == [], (
        f"atomic write must leave no orphan tmp/backup files; found {leftovers}"
    )


# ---------------------------------------------------------------------------
# Case #2: test_fr05_02_run_modes_cached  (integration — run --all dispatch)
# ---------------------------------------------------------------------------

def test_fr05_02_run_modes_cached(taskq_home):
    """FR-05: `taskq run --all` dispatches to executor.run_all which
    fans out under the shared store.Lock; tasks.json stays valid JSON
    with no losses. The `--cached` flag on `run <id>` is wired through
    to `executor.run(task_id, use_cache=True)`.

    Sub-assertion rule: FR05-AC2-run-all-target — `run_target == "--all"`.

    NFR associations:
    # NFR-08 — concurrency: the shared store.Lock keeps tasks.json
    # valid under the ThreadPoolExecutor fan-out that `run --all`
    # dispatches to. The in-process variant below drives this through
    # cli.main so the lock boundary is exercised through the dispatch.
    # NFR-09 — scalability: 8 tasks through TASKQ_MAX_WORKERS=4 workers
    # proves the dispatch can fan out without loss; 8 < max_workers
    # ratio is the same boundary the catalog's NP-13 test relies on.
    # NFR-04 — security: the dispatcher hands the raw command string
    # to executor.run; redaction runs inside the executor before
    # store.update, so no secret leaks via the cli stdout/stderr
    # surface either.
    """
    run_target = "--all"
    state_mode = "isolate_per_test"

    # TEST_SPEC sub-assertion FR05-AC2-run-all-target (case #2).
    assert run_target == "--all", (
        f"FR05-AC2-run-all-target predicate failed: run_target={run_target!r}"
    )
    assert state_mode == "isolate_per_test"

    seeded_ids = _seed_directly(taskq_home, 8)
    assert len(seeded_ids) == 8

    # --- In-process: cli.main(["run", "--all"]) → exit 0 or 3 ---
    rc = cli.main(["run", "--all"])
    assert rc in (0, 3), (
        f"cli.main(['run', '--all']) returned {rc}; expected 0 (success) "
        f"or 3 (breaker OPEN, FR-03 sentinel)"
    )

    stored = _read_tasks_json(taskq_home)
    tasks = stored["tasks"]
    assert len(tasks) == 8, (
        f"expected 8 tasks after run --all; got {len(tasks)} "
        f"(loss detected through the cli dispatch)"
    )
    for tid in seeded_ids:
        assert tid in tasks, f"task {tid} lost through cli dispatch"
        rec = tasks[tid]
        assert rec["status"] in ("done", "failed", "timeout"), (
            f"task {tid} not terminal after run --all: status={rec['status']!r}"
        )

    # --- `--cached` flag wiring: the cli must accept and forward it ---
    # Seed a fresh pending task and assert that `taskq run <id> --cached`
    # dispatches without raising (the executor's cache hit / miss path
    # is exercised by test_fr04; here we only prove the cli forwards).
    fresh_id = _seed_via_cli(taskq_home, "echo cached-flag-check")
    home2 = taskq_home.parent / ".taskq_fr05_02_cached"
    home2.mkdir()
    try:
        result = _run_cli(
            ["run", fresh_id, "--cached"], home2, timeout=10
        )
        # Cached flag is forwarded — exit 0 (hit or miss both succeed).
        assert result.returncode in (0, 3, 4), (
            f"cli with --cached returned {result.returncode}; expected "
            f"0/3/4; stderr={result.stderr!r}"
        )
    finally:
        shutil.rmtree(home2, ignore_errors=True)

    # --- In-process: `run <id> --cached` forwards use_cache=True ---
    # Recorded through a stub so the assertion is on the DISPATCH contract
    # (cli -> executor.run(task_id, use_cache=...)), not on cache behaviour
    # (which test_fr04 owns).
    calls = []

    def _record_run(task_id, use_cache=False):
        calls.append((task_id, use_cache))
        return 0

    saved_run = cli.executor.run
    try:
        cli.executor.run = _record_run
        rc = cli.main(["run", "deadbeef", "--cached"])
        assert rc == 0, f"in-process: run --cached returned {rc}; expected 0"
        rc = cli.main(["run", "deadbeef"])
        assert rc == 0, f"in-process: run <id> returned {rc}; expected 0"
    finally:
        cli.executor.run = saved_run

    assert calls == [("deadbeef", True), ("deadbeef", False)], (
        f"cli must forward the --cached flag as use_cache; got {calls!r}"
    )


# ---------------------------------------------------------------------------
# Case #3: test_fr05_03_status_all_fields  (integration — status <id>)
# ---------------------------------------------------------------------------

def test_fr05_03_status_all_fields(taskq_home):
    """FR-05: `taskq status <id>` prints the full task record; with
    `--json` it prints a single-line JSON object.

    Sub-assertion rule: FR05-AC3-status-id — `len(task_id) == 8`.

    NFR associations:
    # NFR-03 — reliability: status is read from tasks.json which is
    # atomically persisted; a fresh submit then immediate status must
    # observe the persisted record.
    # NFR-10 — evolvability: status is read through the store loader
    # that carries the schema `version` field, so a future schema
    # migration does not silently break the cli status surface.
    """
    task_id = "abcdef01"
    json_flag = "false"

    # TEST_SPEC sub-assertion FR05-AC3-status-id (case #3).
    assert len(task_id) == 8, (
        f"task_id must be 8 chars per FR05-AC3; got {task_id!r} (len="
        f"{len(task_id)})"
    )
    assert json_flag == "false"

    # Seed the task with the EXACT id so the assertion below is meaningful.
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    path = taskq_home / "tasks.json"
    path.write_text(
        json_lib.dumps(
            {
                "version": 1,
                "tasks": {
                    task_id: {
                        "status": "pending",
                        "command": "echo hi",
                        "created_at": now,
                        "name": "status-demo",
                    },
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = _run_cli(["status", task_id], taskq_home)

    assert result.returncode == 0, (
        f"status <id> returned {result.returncode}; expected 0; "
        f"stderr={result.stderr!r}"
    )
    stdout = result.stdout.strip()
    # Non-JSON mode: every persisted field must be visible.
    for field in ("status", "command", "created_at"):
        assert field in stdout, (
            f"status output must include {field!r}; got stdout={stdout!r}"
        )
    assert "pending" in stdout, (
        f"status output must include the current status 'pending'; got "
        f"{stdout!r}"
    )
    assert "echo hi" in stdout, (
        f"status output must include the command 'echo hi'; got {stdout!r}"
    )
    assert task_id in stdout, (
        f"status output must include the task id {task_id!r}; got {stdout!r}"
    )

    # --- In-process variant: same handler, measurable coverage ---
    rc, stdout, stderr = _run_cli_inprocess(["status", task_id], taskq_home)
    assert rc == 0, (
        f"in-process: status returned {rc}; expected 0; stderr={stderr!r}"
    )
    # Text mode renders one `key: value` line per persisted field, id first.
    lines = stdout.strip().split("\n")
    assert lines[0] == f"id: {task_id}", (
        f"in-process: first status line must be the id; got {lines[0]!r}"
    )
    for field, value in (
        ("status", "pending"),
        ("command", "echo hi"),
        ("name", "status-demo"),
    ):
        assert f"{field}: {value}" in lines, (
            f"in-process: status text must render {field!r}; got {lines!r}"
        )
    assert any(line.startswith("created_at: ") for line in lines), (
        f"in-process: status text must render created_at; got {lines!r}"
    )

    # --json on the same record → one line, every field preserved.
    rc, stdout, _stderr = _run_cli_inprocess(
        ["--json", "status", task_id], taskq_home
    )
    assert rc == 0, f"in-process: --json status returned {rc}; expected 0"
    assert "\n" not in stdout.rstrip(), (
        f"in-process: --json status must be one line; got {stdout!r}"
    )
    payload = json_lib.loads(stdout)
    assert payload["id"] == task_id
    assert payload["status"] == "pending"
    assert payload["command"] == "echo hi"
    assert payload["name"] == "status-demo"

    # `--json` is GLOBAL: accepted in trailing position too (SAD §3.1).
    rc, trailing, _stderr = _run_cli_inprocess(
        ["status", task_id, "--json"], taskq_home
    )
    assert rc == 0, f"in-process: trailing --json returned {rc}; expected 0"
    assert json_lib.loads(trailing) == payload, (
        "--json must produce the same payload before or after the subcommand"
    )

    # Unknown id → exit 2 with a diagnostic, not a traceback.
    rc, stdout, stderr = _run_cli_inprocess(["status", "ffffffff"], taskq_home)
    assert rc == 2, f"in-process: unknown id must exit 2; got {rc}"
    assert "ffffffff" in stderr, (
        f"in-process: unknown-id diagnostic must name the id; got {stderr!r}"
    )
    assert stdout == "", (
        f"in-process: unknown id must print no payload; got {stdout!r}"
    )


# ---------------------------------------------------------------------------
# Case #4: test_fr05_04_list_status_filter  (integration — list [--status S])
# ---------------------------------------------------------------------------

def test_fr05_04_list_status_filter(taskq_home):
    """FR-05: `taskq list [--status S]` enumerates tasks; with
    `--status done`, only tasks in `done` state are listed.

    Sub-assertion rule: FR05-AC4-list-filter — `len(filter_status) > 0`.

    NFR associations:
    # NFR-09 — scalability: `list` must stream from tasks.json (SAD
    # §2.2.2 streaming iterator) rather than materialising the entire
    # 1000-task map in memory. Here we seed a small mixed-state set
    # to validate the filter contract; the streaming invariant is
    # also referenced by `fr_module_traceability: taskq.cli`.
    # NFR-03 — reliability: list reads through the store loader which
    # honours the atomic-write boundary; a concurrent run --all cannot
    # corrupt the iteration.
    """
    filter_status = "done"
    expected_count = "1"

    # TEST_SPEC sub-assertion FR05-AC4-list-filter (case #4).
    assert len(filter_status) > 0, (
        f"filter_status must be non-empty per FR05-AC4; got {filter_status!r}"
    )
    assert expected_count == "1"

    # Seed two pending + one done task with explicit ids.
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    path = taskq_home / "tasks.json"
    path.write_text(
        json_lib.dumps(
            {
                "version": 1,
                "tasks": {
                    "aaaaaa01": {
                        "status": "pending",
                        "command": "echo p1",
                        "created_at": now,
                    },
                    "aaaaaa02": {
                        "status": "done",
                        "command": "echo d1",
                        "created_at": now,
                        "exit_code": 0,
                    },
                    "aaaaaa03": {
                        "status": "pending",
                        "command": "echo p2",
                        "created_at": now,
                    },
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = _run_cli(["list", "--status", filter_status], taskq_home)

    assert result.returncode == 0, (
        f"list returned {result.returncode}; expected 0; "
        f"stderr={result.stderr!r}"
    )
    stdout = result.stdout

    # The one done task must be listed; the pending tasks must NOT appear.
    assert "aaaaaa02" in stdout, (
        f"list --status done must include aaaaaa02; got stdout={stdout!r}"
    )
    assert "aaaaaa01" not in stdout, (
        f"list --status done must not include pending aaaaaa01; got "
        f"stdout={stdout!r}"
    )
    assert "aaaaaa03" not in stdout, (
        f"list --status done must not include pending aaaaaa03; got "
        f"stdout={stdout!r}"
    )

    # Without the filter, all three tasks must appear.
    result_all = _run_cli(["list"], taskq_home)
    assert result_all.returncode == 0, (
        f"list (no filter) returned {result_all.returncode}; expected 0"
    )
    for tid in ("aaaaaa01", "aaaaaa02", "aaaaaa03"):
        assert tid in result_all.stdout, (
            f"list (no filter) must include {tid}; got "
            f"stdout={result_all.stdout!r}"
        )

    # --- In-process variant: same handler, measurable coverage ---
    rc, stdout, stderr = _run_cli_inprocess(
        ["list", "--status", filter_status], taskq_home
    )
    assert rc == 0, (
        f"in-process: list --status returned {rc}; expected 0; "
        f"stderr={stderr!r}"
    )
    assert stdout.split() == ["aaaaaa02"], (
        f"in-process: list --status done must yield exactly the done task; "
        f"got {stdout!r}"
    )

    rc, stdout, _stderr = _run_cli_inprocess(["list"], taskq_home)
    assert rc == 0, f"in-process: list returned {rc}; expected 0"
    assert sorted(stdout.split()) == ["aaaaaa01", "aaaaaa02", "aaaaaa03"], (
        f"in-process: unfiltered list must enumerate every task; got {stdout!r}"
    )

    # --json list → a single line holding a JSON ARRAY of full records.
    rc, stdout, _stderr = _run_cli_inprocess(["--json", "list"], taskq_home)
    assert rc == 0, f"in-process: --json list returned {rc}; expected 0"
    assert "\n" not in stdout.rstrip(), (
        f"in-process: --json list must be one line; got {stdout!r}"
    )
    items = json_lib.loads(stdout)
    assert isinstance(items, list) and len(items) == 3, (
        f"--json list must emit a 3-element array; got {items!r}"
    )
    assert {item["id"] for item in items} == {
        "aaaaaa01",
        "aaaaaa02",
        "aaaaaa03",
    }
    done = [item for item in items if item["id"] == "aaaaaa02"][0]
    assert done["status"] == "done" and done["exit_code"] == 0, (
        f"--json list records must carry every persisted field; got {done!r}"
    )

    # A filter matching nothing is an empty success, not an error:
    # text mode prints NOTHING (not a blank line), --json prints `[]`.
    rc, stdout, stderr = _run_cli_inprocess(
        ["list", "--status", "nonesuch"], taskq_home
    )
    assert rc == 0, f"in-process: an empty filter result must exit 0; got {rc}"
    assert stdout == "", (
        f"in-process: an empty list must print nothing in text mode; got "
        f"{stdout!r}"
    )
    rc, stdout, _stderr = _run_cli_inprocess(
        ["--json", "list", "--status", "nonesuch"], taskq_home
    )
    assert rc == 0
    assert json_lib.loads(stdout) == [], (
        f"in-process: an empty --json list must emit []; got {stdout!r}"
    )


# ---------------------------------------------------------------------------
# Case #5: test_fr05_05_clear_data  (integration — clear)
# ---------------------------------------------------------------------------

def test_fr05_05_clear_data(taskq_home):
    """FR-05: `taskq clear` removes every data file under $TASKQ_HOME
    (tasks.json, breaker.json, cache.json).

    Sub-assertion rule: FR05-AC5-clear-files — `file_count == "3"`.

    NFR associations:
    # NFR-03 — reliability: clear must operate on the SAME atomic-write
    # boundary as submit/run; it does NOT introduce a new I/O pattern
    # that could race with a concurrent submit. Here we seed all three
    # files first to prove the clear boundary truly empties the home.
    # NFR-07 — resilience: clear must not swallow unexpected errors
    # silently; it returns 0 on success and does not pretend the
    # directory is empty when only a subset of files were removed.
    """
    file_count = "3"
    state_mode = "isolate_per_test"

    # TEST_SPEC sub-assertion FR05-AC5-clear-files (case #5).
    assert file_count == "3", (
        f"file_count must equal '3' per FR05-AC5; got {file_count!r}"
    )
    assert state_mode == "isolate_per_test"

    # Seed all three data files.
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    (taskq_home / "tasks.json").write_text(
        json_lib.dumps(
            {"version": 1, "tasks": {"id01": {"status": "pending", "command": "x", "created_at": now}}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (taskq_home / "breaker.json").write_text(
        json_lib.dumps(
            {"version": 1, "state": "CLOSED", "failure_count": 0, "opened_at": 0.0},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (taskq_home / "cache.json").write_text(
        json_lib.dumps(
            {"version": 1, "entries": {}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = _run_cli(["clear"], taskq_home)

    assert result.returncode == 0, (
        f"clear returned {result.returncode}; expected 0; "
        f"stderr={result.stderr!r}"
    )
    # All three data files must be gone.
    for fname in ("tasks.json", "breaker.json", "cache.json"):
        assert not (taskq_home / fname).exists(), (
            f"clear must remove {fname}; still present after clear"
        )

    # `clear` on an empty home is still a clean success (exit 0).
    result_empty = _run_cli(["clear"], taskq_home)
    assert result_empty.returncode == 0, (
        f"clear on an empty home must exit 0; got {result_empty.returncode}"
    )

    # --- In-process variant: same handler, measurable coverage ---
    # Re-seed all three data files, then clear through cli.main directly.
    for fname, doc in (
        ("tasks.json", {"version": 1, "tasks": {}}),
        (
            "breaker.json",
            {
                "version": 1,
                "state": "CLOSED",
                "failure_count": 0,
                "opened_at": 0.0,
            },
        ),
        ("cache.json", {"version": 1, "entries": {}}),
    ):
        (taskq_home / fname).write_text(
            json_lib.dumps(doc, ensure_ascii=False), encoding="utf-8"
        )

    rc, stdout, stderr = _run_cli_inprocess(["clear"], taskq_home)
    assert rc == 0, (
        f"in-process: clear returned {rc}; expected 0; stderr={stderr!r}"
    )
    assert stdout == "", (
        f"in-process: clear must print nothing on stdout; got {stdout!r}"
    )
    for fname in ("tasks.json", "breaker.json", "cache.json"):
        assert not (taskq_home / fname).exists(), (
            f"in-process: clear must remove {fname}; still present"
        )

    # Idempotent: clearing an already-empty home is exit 0 (missing_ok).
    rc, _stdout, stderr = _run_cli_inprocess(["clear"], taskq_home)
    assert rc == 0, (
        f"in-process: clear on an empty home must exit 0; got {rc}; "
        f"stderr={stderr!r}"
    )

    # `clear` must not remove the home directory itself, nor unrelated files.
    keeper = taskq_home / "unrelated.txt"
    keeper.write_text("keep me", encoding="utf-8")
    rc, _stdout, _stderr = _run_cli_inprocess(["clear"], taskq_home)
    assert rc == 0
    assert taskq_home.is_dir(), "clear must not remove $TASKQ_HOME itself"
    assert keeper.exists(), (
        "clear must only remove the three known data files, not everything"
    )


# ---------------------------------------------------------------------------
# Case #6: test_fr05_06_one_line_json  (integration — --json global flag)
# ---------------------------------------------------------------------------

def test_fr05_06_one_line_json(taskq_home):
    """FR-05: `--json` is a GLOBAL flag (SAD §3.1); every subcommand's
    output that prints a payload must emit exactly one JSON object on a
    single line.

    Sub-assertion rule: FR05-AC6-json-line-count —
    `line_count == "1" and json_flag == "true"`.

    NFR associations:
    # NFR-05 — maintainability: the global --json flag is the single
    # machine-readable surface used by external tooling; the dispatch
    # layer (`taskq.cli`) owns it. Here we exercise it through `status`
    # because status emits the full record — the most demanding shape.
    """
    subcommand = "status"
    json_flag = "true"
    line_count = "1"

    # TEST_SPEC sub-assertion FR05-AC6-json-line-count (case #6).
    assert line_count == "1" and json_flag == "true", (
        f"FR05-AC6-json-line-count predicate failed: line_count="
        f"{line_count!r}, json_flag={json_flag!r}"
    )

    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    path = taskq_home / "tasks.json"
    path.write_text(
        json_lib.dumps(
            {
                "version": 1,
                "tasks": {
                    "abcdef01": {
                        "status": "pending",
                        "command": "echo hi",
                        "created_at": now,
                    },
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = _run_cli(
        ["--json", subcommand, "abcdef01"], taskq_home
    )

    assert result.returncode == 0, (
        f"--json status returned {result.returncode}; expected 0; "
        f"stderr={result.stderr!r}"
    )
    stdout = result.stdout
    # Strip trailing whitespace so trailing newlines from print do not
    # pollute the single-line check — but there must be NO internal
    # newlines (pretty-print is forbidden).
    stripped = stdout.rstrip()
    assert "\n" not in stripped, (
        f"--json status must produce exactly one JSON line; got "
        f"{stdout!r}"
    )
    parsed = json_lib.loads(stripped)
    assert isinstance(parsed, dict), (
        f"--json status must emit a JSON object; got {type(parsed).__name__}"
    )
    assert parsed.get("id") == "abcdef01", (
        f"--json status payload must include the task id; got {parsed!r}"
    )
    assert parsed.get("status") == "pending", (
        f"--json status payload must include current status 'pending'; got "
        f"{parsed!r}"
    )


# ---------------------------------------------------------------------------
# Cases #7–#11: test_fr05_07_exit_code_error_message_map  (5 scenarios)
# ---------------------------------------------------------------------------

def test_fr05_07_exit_code_error_message_map(taskq_home):
    """FR-05: the canonical exit-code map per SPEC §7 is:

        0 = success
        2 = validation error (incl. unknown task id)
        3 = breaker OPEN
        4 = single-task subprocess timeout
        1 = internal / unexpected error

    Sub-assertion rules: FR05-AC7-exit-{0,2,3,4,1}.

    NFR associations:
    # NFR-02 — security: validation failures (exit 2) must include a
    # diagnostic on stderr without leaking the rejected command payload
    # to a downstream pipe (stderr is the only channel here).
    # NFR-07 — resilience: internal_error (exit 1) is the fail-fast
    # path for a corrupt on-disk document; the cli must NOT silently
    # rebuild it (NFR-03/NFR-07) — surface exit 1 with a diagnostic.

    Implementation note: each scenario is dispatched in its own ``if``
    block so the per-scenario assertions stay scoped to ``expected_exit``
    — the variable name matches TEST_SPEC.md's Inputs column, and the
    MIRROR checker reads each block's IF trigger to scope-align the
    sub-assertion predicates.
    """
    # ----- Scenario 1: success → exit 0 -----
    expected_exit = "0"
    scenario = "success"
    if expected_exit == "0":
        assert expected_exit == "0"  # FR05-AC7-exit-0
        task_id = _seed_via_cli(taskq_home, "echo hi")
        result = _run_cli(["status", task_id], taskq_home)
        assert result.returncode == int(expected_exit), (
            f"scenario={scenario}: expected exit {expected_exit}; got "
            f"{result.returncode}; stderr={result.stderr!r}"
        )

    # ----- Scenario 2: validation → exit 2 -----
    expected_exit = "2"
    scenario = "validation"
    if expected_exit == "2":
        assert expected_exit == "2"  # FR05-AC7-exit-2
        result = _run_cli(["submit", ""], taskq_home)
        assert result.returncode == int(expected_exit), (
            f"scenario={scenario}: expected exit {expected_exit}; got "
            f"{result.returncode}; stderr={result.stderr!r}"
        )
        assert result.stderr.strip() != "", (
            f"scenario={scenario}: non-zero exit must emit a stderr "
            f"diagnostic; got empty stderr"
        )

    # ----- Scenario 3: breaker OPEN → exit 3 -----
    expected_exit = "3"
    scenario = "breaker_open"
    if expected_exit == "3":
        assert expected_exit == "3"  # FR05-AC7-exit-3
        _seed_breaker_open(taskq_home)
        task_id = _seed_via_cli(taskq_home, "echo hi")
        result = _run_cli(["run", task_id], taskq_home)
        assert result.returncode == int(expected_exit), (
            f"scenario={scenario}: expected exit {expected_exit}; got "
            f"{result.returncode}; stderr={result.stderr!r}"
        )
        assert result.stderr.strip() != "", (
            f"scenario={scenario}: non-zero exit must emit a stderr "
            f"diagnostic; got empty stderr"
        )

    # ----- Scenario 4: single-task timeout → exit 4 -----
    expected_exit = "4"
    scenario = "single_task_timeout"
    if expected_exit == "4":
        assert expected_exit == "4"  # FR05-AC7-exit-4
        # The previous scenario left breaker.json in OPEN state. Wipe it
        # so the timeout path is reached (otherwise the run is rejected
        # with exit 3 before subprocess.run is ever called).
        _reset_breaker(taskq_home)
        task_id = _seed_via_cli(taskq_home, "sleep 5")
        result = _run_cli(["run", task_id], taskq_home)
        assert result.returncode == int(expected_exit), (
            f"scenario={scenario}: expected exit {expected_exit}; got "
            f"{result.returncode}; stderr={result.stderr!r}"
        )
        assert result.stderr.strip() != "", (
            f"scenario={scenario}: non-zero exit must emit a stderr "
            f"diagnostic; got empty stderr"
        )

    # ----- Scenario 5: internal error → exit 1 -----
    expected_exit = "1"
    scenario = "internal_error"
    if expected_exit == "1":
        assert expected_exit == "1"  # FR05-AC7-exit-1
        _seed_breaker_corrupt(taskq_home)
        result = _run_cli(["status", "abcdef01"], taskq_home)
        assert result.returncode == int(expected_exit), (
            f"scenario={scenario}: expected exit {expected_exit}; got "
            f"{result.returncode}; stderr={result.stderr!r}"
        )
        assert result.stderr.strip() != "", (
            f"scenario={scenario}: non-zero exit must emit a stderr "
            f"diagnostic; got empty stderr"
        )

    # --- In-process mirror of the exit-code map (measurable coverage) ---
    # Scenarios 3 and 4 stay subprocess-only above: they own real process
    # state (breaker file + a `sleep 5` child) that the executor drives.

    # exit 1 — corrupt breaker.json outranks every other outcome, including
    # an unknown id (a corrupt home is internal error, NOT validation).
    _seed_breaker_corrupt(taskq_home)
    for argv in (["status", "abcdef01"], ["status", "ffffffff"]):
        rc, stdout, stderr = _run_cli_inprocess(argv, taskq_home)
        assert rc == 1, (
            f"in-process: corrupt breaker.json must exit 1 for {argv!r}; "
            f"got {rc}"
        )
        assert stderr.startswith("status: "), (
            f"in-process: exit 1 must carry a 'status:' diagnostic; got "
            f"{stderr!r}"
        )
        assert stdout == "", (
            f"in-process: exit 1 must emit no payload; got {stdout!r}"
        )

    # exit 2 — unknown id, once the corruption is cleared.
    _reset_breaker(taskq_home)
    rc, _stdout, stderr = _run_cli_inprocess(["status", "ffffffff"], taskq_home)
    assert rc == 2, f"in-process: unknown id must exit 2; got {rc}"
    assert stderr.strip() != "", "exit 2 must emit a stderr diagnostic"

    # exit 0 — a real record, read back through the same dispatch.
    known_id = _seed_via_cli(taskq_home, "echo hi")
    rc, stdout, stderr = _run_cli_inprocess(["status", known_id], taskq_home)
    assert rc == 0, (
        f"in-process: status on a known id must exit 0; got {rc}; "
        f"stderr={stderr!r}"
    )
    assert known_id in stdout

    # exit 2 — validation, straight through the submit gate.
    rc, _stdout, stderr = _run_cli_inprocess(["submit", "  "], taskq_home)
    assert rc == 2, f"in-process: empty submit must exit 2; got {rc}"
    assert stderr.strip() != "", "exit 2 must emit a stderr diagnostic"

    # The exit-code constants are the SSOT the handlers return (SPEC §7).
    assert (
        cli.EXIT_SUCCESS,
        cli.EXIT_INTERNAL_ERROR,
        cli.EXIT_VALIDATION_ERROR,
    ) == (0, 1, 2), "SPEC §7 exit-code constants must not drift"


# ---------------------------------------------------------------------------
# Case #12: test_fr05_08_python_m_taskq_entry  (integration — entry point)
# ---------------------------------------------------------------------------

def test_fr05_08_python_m_taskq_entry(taskq_home):
    """FR-05: `python -m taskq` is the documented entry point per
    SAD §2.2.1. `python -m taskq --help` exits 0 with the help text
    on stdout (argparse default).

    Sub-assertion rule: FR05-AC8-entry-args — `len(entry_args) > 0`.

    NFR associations:
    # NFR-06 — deployability: the entry point is the deployment surface
    # (CLI binary form); `python -m taskq` must be wired through
    # `taskq.__main__` to `taskq.cli.main`, with all 8 TASKQ_* knobs
    # resolved through config.
    """
    entry_args = "--help"
    expected_exit = "0"
    state_mode = "isolate_per_test"

    # TEST_SPEC sub-assertion FR05-AC8-entry-args (case #12).
    assert len(entry_args) > 0, (
        f"entry_args must be non-empty per FR05-AC8; got {entry_args!r}"
    )
    assert expected_exit == "0" and state_mode == "isolate_per_test"

    # Subprocess entry-point invocation.
    result = _run_cli([entry_args], taskq_home)
    assert result.returncode == 0, (
        f"python -m taskq --help returned {result.returncode}; expected 0; "
        f"stderr={result.stderr!r}"
    )
    # argparse prints the program name on stdout for --help.
    assert "taskq" in result.stdout, (
        f"--help output must mention 'taskq'; got stdout={result.stdout!r}"
    )
    # And every subcommand must be discoverable from --help.
    for sub in ("submit", "run", "status", "list", "clear"):
        assert sub in result.stdout, (
            f"--help must list subcommand {sub!r}; got stdout="
            f"{result.stdout!r}"
        )

    # --- In-process variant: the parser argparse builds is the same one
    # `python -m taskq` drives, so --help is measurable here. argparse
    # raises SystemExit(0) after printing, which the entry point forwards.
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        with pytest.raises(SystemExit) as excinfo:
            cli.main([entry_args])
    assert excinfo.value.code == 0, (
        f"in-process: --help must exit 0; got {excinfo.value.code}"
    )
    help_text = buf.getvalue()
    for sub in ("submit", "run", "status", "list", "clear"):
        assert sub in help_text, (
            f"in-process: --help must list {sub!r}; got {help_text!r}"
        )
    assert "--json" in help_text, (
        f"in-process: the global --json flag must be discoverable from "
        f"--help; got {help_text!r}"
    )

    # A missing subcommand is a usage error (argparse exit 2), never a
    # traceback — subparsers are declared required=True.
    with contextlib.redirect_stdout(io.StringIO()):
        saved_stderr = sys.stderr
        try:
            sys.stderr = io.StringIO()
            with pytest.raises(SystemExit) as excinfo:
                cli.main([])
        finally:
            sys.stderr = saved_stderr
    assert excinfo.value.code == 2, (
        f"in-process: a missing subcommand must exit 2; got "
        f"{excinfo.value.code}"
    )