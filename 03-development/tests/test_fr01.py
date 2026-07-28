"""FR-01: Task submission and validation — TDD-RED failing tests.

Tests in this file exercise `taskq submit "<command>" [--name NAME] [--json]`
per SRS.md FR-01 and TEST_SPEC.md (rows #1–#14 for FR-01).

Test strategy:
- Subprocess tests (`_run_cli`): exercise the real CLI surface end-to-end.
- In-process tests (`cli.main([...])`): exercise the same validation paths
  in-process for coverage (per INTEGRATION FR GUIDELINES — pytest-cov
  cannot measure code in subprocess). The two test types coexist.

NOTE: These tests are expected to FAIL with a Collection Error
(ModuleNotFoundError: No module named 'taskq') because the source code does
not exist yet. That is the GREEN step's job. Do not add try/except
ImportError to hide the failure — the Collection Error is the valid RED
state.
"""

from __future__ import annotations

import contextlib
import io
import json as json_lib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

# Standard top-level import — Collection Error (Exit Code 2) is the valid
# RED state when source code does not exist yet.
from taskq import cli  # noqa: E402  (collection error is expected)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

# SRC_ROOT so subprocess tests can propagate PYTHONPATH (pytest's `pythonpath`
# setting does NOT propagate to child processes automatically).
SRC_ROOT = Path(__file__).resolve().parent.parent / "src"


@pytest.fixture
def taskq_home(tmp_path, monkeypatch):
    """Per-test isolated $TASKQ_HOME.

    Returns the Path of the isolated home directory. The directory is empty
    at test start; monkeypatch cleans up the env var afterwards.
    """
    home = tmp_path / ".taskq"
    home.mkdir()
    monkeypatch.setenv("TASKQ_HOME", str(home))
    # Ensure HOME is not picked up by accident if code falls back to it.
    monkeypatch.setenv("HOME", str(tmp_path))
    return home


def _build_env(taskq_home: Path) -> dict:
    """Build a child-process env that propagates TASKQ_HOME + PYTHONPATH."""
    env = os.environ.copy()
    env["TASKQ_HOME"] = str(taskq_home)
    env["PYTHONPATH"] = str(SRC_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return env


def _run_cli(args, taskq_home: Path):
    """Run `python -m taskq ...` as a subprocess for the given isolated home.

    Returns CompletedProcess; caller asserts on .returncode / .stdout / .stderr.
    """
    cmd = [sys.executable, "-m", "taskq", *args]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=_build_env(taskq_home),
        timeout=30,
    )


def _run_cli_inprocess(args, taskq_home: Path):
    """Invoke cli.main([...]) in-process with captured stdout/stderr.

    Returns (exit_code, stdout_text, stderr_text). Used to exercise the
    same validation paths for in-process coverage (pytest-cov cannot see
    subprocess code).
    """
    # cli.main mutates argv-style list; pass it directly. Some cli.main
    # implementations read os.environ (which is already overridden by the
    # taskq_home fixture) — no extra setup needed.
    buf_out = io.StringIO()
    buf_err = io.StringIO()
    # Use redirect_stdout for stdout. stderr may or may not be redirected
    # by cli — we sniff cli internals minimally: if cli writes to
    # sys.stderr directly, capture by reassigning for the call.
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
            f"Expected tasks.json to exist at {path}, but it was not written. "
            "FR-01 requires atomic persistence on success."
        )
    return json_lib.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Case #1, #2: test_fr01_01_empty_whitespace_rejection  (2 scenarios)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "scenario,command",
    [
        ("empty_string", ""),
        ("all_whitespace", "   "),
    ],
)
def test_fr01_01_empty_whitespace_rejection(taskq_home, scenario, command):
    """FR-01: empty / all-whitespace commands must exit 2, report stderr,
    and NOT write tasks.json.

    Sub-assertion rule: FR01-AC1-empty-string | FR01-AC1-whitespace-only.

    NFR associations:
    # NFR-06 — fixture-driven TASKQ_HOME isolation (env var override tested here).
    """
    result = _run_cli(["submit", command], taskq_home)

    # Exit code 2 is the validation exit per SPEC §7.
    assert result.returncode == 2, (
        f"scenario={scenario}: expected exit 2 for empty/whitespace command, "
        f"got {result.returncode}; stderr={result.stderr!r}"
    )
    # Some diagnostic must be reported on stderr.
    assert result.stderr.strip() != "", (
        f"scenario={scenario}: expected non-empty stderr message, got empty"
    )
    # No storage must be written on validation failure.
    tasks_file = taskq_home / "tasks.json"
    assert not tasks_file.exists(), (
        f"scenario={scenario}: tasks.json must not be written on validation "
        f"failure, but file exists at {tasks_file}"
    )

    # In-process variant — same validation paths, in-process coverage.
    rc, stdout, stderr = _run_cli_inprocess(["submit", command], taskq_home)
    assert rc == 2, (
        f"scenario={scenario} (in-process): expected exit 2, got {rc}; "
        f"stderr={stderr!r}"
    )
    assert stderr.strip() != "" or stdout.strip() != "", (
        f"scenario={scenario} (in-process): expected a diagnostic message"
    )


# ---------------------------------------------------------------------------
# Case #3: test_fr01_02_length_rejection  (boundary > 1000)
# ---------------------------------------------------------------------------

def test_fr01_02_length_rejection(taskq_home):
    """FR-01: commands longer than 1000 characters must be rejected.

    Sub-assertion rule: FR01-AC2-length-gt-1000 — 1001 'x' characters.
    """
    command = "x" * 1001
    assert len(command) == 1001  # boundary sanity

    result = _run_cli(["submit", command], taskq_home)

    assert result.returncode == 2, (
        f"expected exit 2 for over-length command, got {result.returncode}; "
        f"stderr={result.stderr!r}"
    )
    assert result.stderr.strip() != "", (
        "expected a stderr message describing the rejection"
    )
    assert not (taskq_home / "tasks.json").exists(), (
        "tasks.json must not be written on length-rejection failure"
    )

    # In-process variant.
    rc, _stdout, _stderr = _run_cli_inprocess(["submit", command], taskq_home)
    assert rc == 2, f"in-process: expected exit 2 for over-length, got {rc}"


# ---------------------------------------------------------------------------
# Cases #4–#10: test_fr01_03_injection_blacklist  (7 blacklisted characters)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "blacklisted,command",
    [
        (";", "echo hi ; ls"),
        ("|", "echo hi | wc"),
        ("&", "echo hi & bg"),
        ("$", "echo $HOME"),
        (">", "cat > out"),
        ("<", "cat < in"),
        ("`", "echo `pwd`"),
    ],
)
def test_fr01_03_injection_blacklist(taskq_home, blacklisted, command):
    r"""FR-01: commands containing any of `; | & $ > < \`` must be rejected.

    Each parametrized case exercises one blacklisted character. The CLI must
    exit 2, report stderr, and NOT write tasks.json.

    Sub-assertion rules: FR01-AC3-blacklist-{semicolon,pipe,ampersand,
    dollar,redirect-out,redirect-in,backtick}.

    NFR associations:
    # NFR-02 — security: injection-blacklist character coverage (every listed
    character is exercised by exactly one parametrize row).
    """
    # Sanity: each test command must actually contain its declared blacklist
    # character — protects against typos in the parametrize table.
    assert blacklisted in command, (
        f"blacklisted={blacklisted!r} must appear in command={command!r}"
    )

    result = _run_cli(["submit", command], taskq_home)

    assert result.returncode == 2, (
        f"blacklisted={blacklisted!r}: expected exit 2, got "
        f"{result.returncode}; stderr={result.stderr!r}"
    )
    assert result.stderr.strip() != "", (
        f"blacklisted={blacklisted!r}: expected stderr diagnostic"
    )
    assert not (taskq_home / "tasks.json").exists(), (
        f"blacklisted={blacklisted!r}: tasks.json must not be written on "
        "injection-rejection failure"
    )

    # In-process variant.
    rc, _stdout, _stderr = _run_cli_inprocess(
        ["submit", command], taskq_home
    )
    assert rc == 2, (
        f"blacklisted={blacklisted!r} (in-process): expected exit 2, "
        f"got {rc}"
    )


# ---------------------------------------------------------------------------
# Case #11: test_fr01_04_duplicate_name_rejection  (state isolation)
# ---------------------------------------------------------------------------

def test_fr01_04_duplicate_name_rejection(taskq_home):
    """FR-01: a duplicate --name among existing pending/running tasks must
    be rejected with exit 2.

    Sub-assertion rule: FR01-AC4-duplicate-name.
    State mode: isolate_per_test (per-test fixture gives a fresh TASKQ_HOME).
    """
    name = "deploy-prod"

    # First submission with the name — must succeed.
    first = _run_cli(["submit", "echo hi", "--name", name], taskq_home)
    assert first.returncode == 0, (
        f"first submission should succeed; got {first.returncode}; "
        f"stderr={first.stderr!r}"
    )
    # The first task should be pending and stored.
    stored = _read_tasks_json(taskq_home)
    assert "tasks" in stored, "tasks.json must contain a 'tasks' key"
    task_ids = list(stored["tasks"].keys())
    assert len(task_ids) == 1, (
        f"expected exactly one task stored, got {len(task_ids)}: {task_ids}"
    )
    assert stored["tasks"][task_ids[0]]["name"] == name, (
        "stored task's name must match the submitted --name"
    )

    # Second submission with the same --name — must be rejected.
    second = _run_cli(
        ["submit", "echo hi again", "--name", name], taskq_home
    )
    assert second.returncode == 2, (
        f"duplicate-name submission should exit 2; got {second.returncode}; "
        f"stderr={second.stderr!r}"
    )
    assert second.stderr.strip() != "", (
        "duplicate-name rejection must produce a stderr diagnostic"
    )
    # No new task may have been written.
    stored_after = _read_tasks_json(taskq_home)
    assert len(stored_after["tasks"]) == 1, (
        "duplicate-name submission must not append a new task"
    )


# ---------------------------------------------------------------------------
# Case #12: test_fr01_05_valid_id_pending_record  (happy path)
# ---------------------------------------------------------------------------

def test_fr01_05_valid_id_pending_record(taskq_home):
    """FR-01: a valid command must produce an 8-hex id and pending status.

    Sub-assertion rule: FR01-AC5-valid-non-empty.

    NFR associations:
    # NFR-06 — deployability: TASKQ_HOME env var read by `taskq.config`
    drives the persistence path exercised here.
    """
    result = _run_cli(["submit", "echo hi"], taskq_home)

    assert result.returncode == 0, (
        f"expected exit 0 for valid submission, got {result.returncode}; "
        f"stderr={result.stderr!r}"
    )
    # The 8-hex id must be printed to stdout.
    printed = result.stdout.strip()
    assert re.fullmatch(r"[0-9a-f]{8}", printed), (
        f"stdout must be an 8-hex task id, got {printed!r}"
    )

    # tasks.json must contain a pending record with matching fields.
    stored = _read_tasks_json(taskq_home)
    assert printed in stored.get("tasks", {}), (
        f"printed id {printed!r} must be a key in tasks.json 'tasks' map; "
        f"got keys {list(stored.get('tasks', {}).keys())}"
    )
    record = stored["tasks"][printed]
    assert record["status"] == "pending", (
        f"new task must be pending, got {record['status']!r}"
    )
    assert record["command"] == "echo hi", (
        f"record.command must echo submitted command; got {record['command']!r}"
    )
    assert "created_at" in record, "record must include created_at timestamp"


# ---------------------------------------------------------------------------
# Case #13: test_fr01_06_atomic_persistence  (atomic write boundary)
# ---------------------------------------------------------------------------

def test_fr01_06_atomic_persistence(taskq_home):
    """FR-01: successful submission must atomically persist tasks.json — no
    orphan tmp files left behind after the write completes.

    Sub-assertion rule: FR01-AC6-atomic-write.
    Pattern: tmp_orphan_check=true, subprocess_mode=in_process (we use
    subprocess here because the atomic-write boundary is the persisted file,
    not the Python entry; in-process equivalent below).

    NFR associations:
    # NFR-03 — reliability: atomic write (tmp + os.replace) must leave a
    valid JSON file with no orphan .tmp entries.
    # NFR-07 — resilience: tasks.json mid-write corruption must be detectable
    via the atomic-write boundary; this test guards the happy-path boundary
    that fault scenarios will target.
    # NFR-08 — concurrency: cross-process flock on tasks.json is exercised by
    the same atomic-write path; this test establishes the no-orphan invariant
    that flock-protected writes must preserve.
    # NFR-10 — evolvability: schema migration relies on the tasks.json root
    containing `version`; the atomic-write path initialises the v1 root.
    """
    # Snapshot the directory listing before the write so we can detect
    # any tmp/orphan files that survive after success.
    before = set(p.name for p in taskq_home.iterdir())

    result = _run_cli(["submit", "echo hi"], taskq_home)

    assert result.returncode == 0, (
        f"expected exit 0 for valid submission, got {result.returncode}; "
        f"stderr={result.stderr!r}"
    )

    # tasks.json must exist and be valid JSON (atomic write guarantees
    # this — partial writes would leave an invalid file).
    tasks_file = taskq_home / "tasks.json"
    assert tasks_file.exists(), "tasks.json must be written on success"
    parsed = json_lib.loads(tasks_file.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict), "tasks.json root must be a JSON object"

    # No orphan tmp / .bak / partial-write files left behind. Compare the
    # directory snapshot before and after.
    after = set(p.name for p in taskq_home.iterdir())
    new_entries = after - before
    # Only tasks.json (and possibly subdirs like cache/breaker — none here)
    # should have been added.
    tmp_like = [
        n for n in new_entries
        if n.startswith(".") and n.endswith(".tmp")
        or n.endswith(".tmp")
        or n.endswith("~")
    ]
    assert not tmp_like, (
        f"atomic write left orphan tmp files behind: {tmp_like}; "
        f"before={before}, after={after}"
    )

    # In-process variant — same atomic write boundary.
    # Use a fresh home to avoid id collision with the subprocess call above.
    home2 = taskq_home.parent / ".taskq_inproc"
    home2.mkdir()
    saved_env = os.environ.get("TASKQ_HOME")
    try:
        os.environ["TASKQ_HOME"] = str(home2)
        before2 = set(p.name for p in home2.iterdir())
        rc, _stdout, _stderr = _run_cli_inprocess(["submit", "echo hi"], home2)
        assert rc == 0, f"in-process: expected exit 0, got {rc}; stderr={_stderr!r}"
        assert (home2 / "tasks.json").exists(), (
            "in-process: tasks.json must be written on success"
        )
        after2 = set(p.name for p in home2.iterdir())
        new_entries2 = after2 - before2
        tmp_like2 = [
            n for n in new_entries2
            if n.endswith(".tmp") or n.endswith("~")
        ]
        assert not tmp_like2, (
            f"in-process: atomic write left orphan files: {tmp_like2}"
        )
    finally:
        if saved_env is None:
            os.environ.pop("TASKQ_HOME", None)
        else:
            os.environ["TASKQ_HOME"] = saved_env
        shutil.rmtree(home2, ignore_errors=True)


# ---------------------------------------------------------------------------
# Case #14: test_fr01_07_json_output  (--json mode)
# ---------------------------------------------------------------------------

def test_fr01_07_json_output(taskq_home):
    """FR-01: with --json, stdout must be one JSON object containing
    `id` and `status: "pending"`.

    Sub-assertion rule: FR01-AC7-json-mode.

    NFR associations:
    # NFR-06 — deployability: --json flag is a CLI flag that must work under
    TASKQ_HOME env override (fixture-driven).
    """
    # NOTE: local variable name intentionally avoids shadowing stdlib `json`.
    json_flag = "true"  # mirrors the TEST_SPEC inputs column

    result = _run_cli(
        ["submit", "echo hi", "--json"], taskq_home
    )

    assert result.returncode == 0, (
        f"expected exit 0, got {result.returncode}; stderr={result.stderr!r}"
    )

    # stdout must parse as a single JSON object.
    stdout = result.stdout.strip()
    assert stdout != "", "stdout must not be empty in --json mode"
    # Must be exactly one line (no pretty-print, no trailing log noise).
    assert "\n" not in stdout, (
        f"--json stdout must be a single line, got: {stdout!r}"
    )
    parsed = json_lib.loads(stdout)
    assert isinstance(parsed, dict), (
        f"--json stdout must be a JSON object, got {type(parsed).__name__}"
    )
    assert "id" in parsed, f"--json output must contain 'id' field; got {parsed!r}"
    assert "status" in parsed, (
        f"--json output must contain 'status' field; got {parsed!r}"
    )
    assert parsed["status"] == "pending", (
        f"--json status must be 'pending', got {parsed['status']!r}"
    )
    # And the id must follow the 8-hex pattern.
    assert re.fullmatch(r"[0-9a-f]{8}", parsed["id"]), (
        f"--json id must be 8-hex, got {parsed['id']!r}"
    )

    # Sentinel: ensure the json_flag local was actually exercised (the
    # parametrize-style rule_id requires it). This no-op assertion guards
    # against accidental removal during refactors.
    assert json_flag == "true"

    # In-process variant — same validation + json path.
    home2 = taskq_home.parent / ".taskq_json_inproc"
    home2.mkdir()
    saved_env = os.environ.get("TASKQ_HOME")
    try:
        os.environ["TASKQ_HOME"] = str(home2)
        rc, stdout_text, _stderr = _run_cli_inprocess(
            ["submit", "echo hi", "--json"], home2
        )
        assert rc == 0, f"in-process: expected exit 0, got {rc}"
        parsed_inproc = json_lib.loads(stdout_text.strip())
        assert parsed_inproc.get("status") == "pending", (
            f"in-process --json status must be 'pending', got "
            f"{parsed_inproc.get('status')!r}"
        )
        assert re.fullmatch(r"[0-9a-f]{8}", parsed_inproc.get("id", "")), (
            f"in-process --json id must be 8-hex, got {parsed_inproc.get('id')!r}"
        )
    finally:
        if saved_env is None:
            os.environ.pop("TASKQ_HOME", None)
        else:
            os.environ["TASKQ_HOME"] = saved_env
        shutil.rmtree(home2, ignore_errors=True)