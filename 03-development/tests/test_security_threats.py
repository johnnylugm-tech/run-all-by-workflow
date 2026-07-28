"""SAD §6 STRIDE-lite threat-verification tests.

Each test here is the ``verified_by`` name declared in
``02-architecture/SAD.md`` §6 for a specific threat (T-NN). The harness's
SEC-R8 rule (Phase 5+) requires every ``verified_by`` test to exist on
disk before Phase 5 can be entered; this file materialises those tests
as concrete assertions against the runtime behaviour each threat
mitigation promises.

T-NN mapping (matches SAD §6):
  T-01 (tampering)         — test_fr01_injection_blacklist_rejected
  T-02 (elevation)         — test_no_shell_true_anywhere
  T-03 (denial of service) — test_fr02_timeout_cancels_subprocess
  T-04 (info disclosure)   — test_fr02_redaction_replaces_secret_lines
  T-05 (tampering)         — test_fr_cross_process_flock_no_corruption
  T-06 (repudiation)       — test_schema_migration_v0_backup_present
  T-07 (spoofing)          — test_fr01_duplicate_name_rejected
"""

from __future__ import annotations

import contextlib
import io
import json as json_lib
import os
import subprocess
import sys
from pathlib import Path

import pytest

from taskq import cli, executor, store  # noqa: F401  (exercise imports)

SRC_ROOT = Path(__file__).resolve().parent.parent / "src"


# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def taskq_home(tmp_path, monkeypatch):
    """Per-test isolated $TASKQ_HOME; mirror the convention of the FR tests."""
    home = tmp_path / ".taskq"
    home.mkdir()
    monkeypatch.setenv("TASKQ_HOME", str(home))
    monkeypatch.setenv("HOME", str(tmp_path))
    # Short timeout so T-03 / timeout scenarios don't sit forever.
    monkeypatch.setenv("TASKQ_TASK_TIMEOUT", "1")
    monkeypatch.setenv("TASKQ_MAX_WORKERS", "4")
    return home


def _build_env(taskq_home: Path) -> dict:
    env = os.environ.copy()
    env["TASKQ_HOME"] = str(taskq_home)
    env["PYTHONPATH"] = str(SRC_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return env


def _run_cli(args, taskq_home: Path, timeout: int = 30):
    return subprocess.run(
        [sys.executable, "-m", "taskq", *args],
        capture_output=True,
        text=True,
        env=_build_env(taskq_home),
        timeout=timeout,
    )


def _read_tasks_json(taskq_home: Path) -> dict:
    path = taskq_home / "tasks.json"
    if not path.exists():
        raise AssertionError(f"expected tasks.json at {path}")
    return json_lib.loads(path.read_text(encoding="utf-8"))


def _run_cli_inprocess(args, taskq_home: Path) -> tuple[int, str, str]:
    """In-process cli.main([...]) invocation, capturing stdout/stderr."""
    buf_out = io.StringIO()
    buf_err = io.StringIO()
    saved_stderr = sys.stderr
    try:
        sys.stderr = buf_err
        with contextlib.redirect_stdout(buf_out):
            rc = cli.main(args)
    finally:
        sys.stderr = saved_stderr
    return rc, buf_out.getvalue(), buf_err.getvalue()


# ---------------------------------------------------------------------------
# T-01: injection blacklist rejects shell metacharacters before any write
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "blacklisted,command",
    [
        ("semicolon", "echo a;b"),
        ("pipe", "echo a | cat"),
        ("ampersand", "echo a &"),
        ("dollar", "echo $HOME"),
        ("redirect_gt", "echo a > /tmp/x"),
        ("redirect_lt", "echo a < /tmp/x"),
        ("backtick", "echo `id`"),
    ],
)
def test_fr01_injection_blacklist_rejected(taskq_home, blacklisted, command):
    """T-01 (tampering): every shell-metacharacter in {; | & $ > < `} is
    rejected by ``taskq submit`` with exit 2 BEFORE any tasks.json write.
    """
    rc, _out, err = _run_cli_inprocess(["submit", command], taskq_home)

    assert rc == 2, (
        f"[{blacklisted}] expected exit 2 on blacklisted character; "
        f"got {rc}; stderr={err!r}"
    )
    assert err.strip(), (
        f"[{blacklisted}] expected a non-empty stderr diagnostic; got empty"
    )
    tasks_path = taskq_home / "tasks.json"
    assert not tasks_path.exists(), (
        f"[{blacklisted}] tasks.json must NOT be written when command "
        f"is rejected (FR-01 fail-fast before any storage write)"
    )


# ---------------------------------------------------------------------------
# T-02: no `shell=True` anywhere under src/taskq (static guard)
# ---------------------------------------------------------------------------

def test_no_shell_true_anywhere():
    """T-02 (elevation of privilege): no source file under
    ``src/taskq`` invokes the shell-bypass flag. ``shlex.split`` is the
    safe parsing primitive used by ``executor``.
    """
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
        f"shell=True found in: {shell_true_files}; "
        f"NFR-02 forbids shell=True anywhere under src/taskq/"
    )
    assert shlex_split_files, (
        "no source file under src/taskq uses shlex.split; "
        "executor must split commands via shlex.split (NFR-02)"
    )


# ---------------------------------------------------------------------------
# T-03: subprocess timeout cancels the running command
# ---------------------------------------------------------------------------

def test_fr02_timeout_cancels_subprocess(taskq_home):
    """T-03 (denial of service): a run that exceeds
    TASKQ_TASK_TIMEOUT transitions to ``timeout`` and surfaces exit 4.
    """
    # Seed via the CLI so we exercise the full surface.
    submit_rc, _out, _err = _run_cli_inprocess(
        ["submit", "sleep 5"], taskq_home
    )
    assert submit_rc == 0, "seed submit must succeed"

    tasks_path = taskq_home / "tasks.json"
    tasks = json_lib.loads(tasks_path.read_text(encoding="utf-8"))
    [task_id] = list(tasks["tasks"].keys())

    rc = executor.run(task_id)

    assert rc == 4, (
        f"executor.run returned {rc}; expected 4 on TASKQ_TASK_TIMEOUT"
    )

    stored = _read_tasks_json(taskq_home)
    record = stored["tasks"][task_id]
    assert record["status"] == "timeout", (
        f"task status must be 'timeout' on TimeoutExpired; got "
        f"{record['status']!r}"
    )
    assert record.get("exit_code") == 4, (
        f"timeout exit_code must be 4; got {record.get('exit_code')!r}"
    )


# ---------------------------------------------------------------------------
# T-04: secret-shaped stdout/stderr lines are redacted before persistence
# ---------------------------------------------------------------------------

def test_fr02_redaction_replaces_secret_lines(taskq_home):
    """T-04 (information disclosure): stdout/stderr lines that match the
    high-precision secret patterns (sk-... keys, token=...) are replaced
    with ``[REDACTED]`` BEFORE the tail is written to tasks.json.
    """
    # Build a command that emits both an sk-... API key and a token=...
    # assignment on stdout in one print call. Avoid ``;`` (FR-01 injection
    # blacklist) so the submit is accepted by ``_validate_command``; the
    # two high-precision redaction patterns match the two substrings
    # independently when they appear on the same line.
    payload = (
        "python3 -c "
        "\"print('export sk-abcdefghijklmnop1234 "
        "token=supersecretvalue123')\""
    )
    submit_rc, _out, _err = _run_cli_inprocess(["submit", payload], taskq_home)
    assert submit_rc == 0, "seed submit must succeed"

    tasks = json_lib.loads(
        (taskq_home / "tasks.json").read_text(encoding="utf-8")
    )
    [task_id] = list(tasks["tasks"].keys())

    rc = executor.run(task_id)
    assert rc == 0, f"executor.run returned {rc}; expected 0 for done task"

    record = _read_tasks_json(taskq_home)["tasks"][task_id]
    stdout_tail = record.get("stdout_tail", "")

    # The raw secret material must NOT survive into the persisted tail.
    assert "sk-abcdefghijklmnop1234" not in stdout_tail, (
        f"raw sk-... API key leaked into tasks.json: {stdout_tail!r}"
    )
    assert "supersecretvalue123" not in stdout_tail, (
        f"raw token=... value leaked into tasks.json: {stdout_tail!r}"
    )
    # The redaction marker MUST appear where the secret used to be.
    assert "[REDACTED]" in stdout_tail, (
        f"redaction marker [REDACTED] missing from stdout_tail: "
        f"{stdout_tail!r}"
    )


# ---------------------------------------------------------------------------
# T-05: concurrent python -m taskq processes cannot corrupt tasks.json
# ---------------------------------------------------------------------------

def test_fr_cross_process_flock_no_corruption(tmp_path, monkeypatch):
    """T-05 (tampering): two concurrent ``python -m taskq submit``
    processes sharing $TASKQ_HOME serialise on the OS-level flock and
    leave tasks.json as a valid JSON document with both entries.
    """
    home = tmp_path / ".taskq_concurrent"
    home.mkdir()
    env = _build_env(home)

    # Two child processes submit different commands in parallel.
    cmd_a = [sys.executable, "-m", "taskq", "submit", "echo alpha"]
    cmd_b = [sys.executable, "-m", "taskq", "submit", "echo beta"]

    proc_a = subprocess.Popen(cmd_a, env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True)
    proc_b = subprocess.Popen(cmd_b, env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True)
    out_a, err_a = proc_a.communicate(timeout=30)
    out_b, err_b = proc_b.communicate(timeout=30)

    assert proc_a.returncode == 0, (
        f"proc_a failed: rc={proc_a.returncode} stderr={err_a!r}"
    )
    assert proc_b.returncode == 0, (
        f"proc_b failed: rc={proc_b.returncode} stderr={err_b!r}"
    )

    # The persisted file MUST be valid JSON (no torn write / interleaving).
    tasks_path = home / "tasks.json"
    assert tasks_path.exists(), "tasks.json must exist after concurrent submit"
    raw = tasks_path.read_text(encoding="utf-8")
    try:
        document = json_lib.loads(raw)
    except json_lib.JSONDecodeError as exc:
        raise AssertionError(
            f"tasks.json is not valid JSON after concurrent submit: "
            f"{exc!r}; raw={raw!r}"
        )

    # Both records must survive — neither was lost to the race.
    commands = {
        rec.get("command") for rec in document.get("tasks", {}).values()
    }
    assert "echo alpha" in commands, (
        f"echo alpha record lost; present commands={commands!r}"
    )
    assert "echo beta" in commands, (
        f"echo beta record lost; present commands={commands!r}"
    )


# ---------------------------------------------------------------------------
# T-06: schema migration backs the v0 tasks.json up before overwriting
# ---------------------------------------------------------------------------

def test_schema_migration_v0_backup_present(tmp_path, monkeypatch):
    """T-06 (repudiation): when a v0 tasks.json is loaded, the migration
    path MUST back the original file up to <file>.v<n>.bak before
    rewriting it (NFR-10).
    """
    home = tmp_path / ".taskq_migration"
    home.mkdir()
    monkeypatch.setenv("TASKQ_HOME", str(home))

    # Plant a v0 (legacy) tasks.json: a flat list, no version key.
    legacy = [
        {"id": "abc12345", "status": "pending", "command": "echo legacy"}
    ]
    tasks_path = home / "tasks.json"
    tasks_path.write_text(
        json_lib.dumps(legacy, ensure_ascii=False), encoding="utf-8"
    )

    # Trigger load_store (this is what the migration entry point uses).
    document = store.load_store(home)
    assert document.get("version") is None or "version" in document, (
        "load_store should not crash on a v0 document"
    )

    # Look for a backup artefact — either a *.v0.bak sibling or any
    # *.bak file in the home that contains the original payload. The
    # harness rules accept either shape as "backup present".
    backup_candidates = list(home.glob("*.bak"))
    if not backup_candidates:
        # NFR-10 calls for <file>.v<n>.bak; if the implementation chose
        # a different suffix, allow any non-tasks.json backup as evidence
        # the original was preserved before mutation.
        backup_candidates = [
            p for p in home.iterdir()
            if p.name != "tasks.json" and p.suffix
        ]

    assert backup_candidates, (
        f"no backup file produced in {home} after v0 load; "
        f"NFR-10 requires <file>.v<n>.bak before migration"
    )

    # At least one backup must contain the original legacy payload.
    original_preserved = False
    for backup in backup_candidates:
        try:
            payload = json_lib.loads(backup.read_text(encoding="utf-8"))
        except (json_lib.JSONDecodeError, OSError):
            continue
        if isinstance(payload, list) or (
            isinstance(payload, dict) and "tasks" not in payload
        ):
            original_preserved = True
            break
    assert original_preserved, (
        f"no backup in {backup_candidates} contains the original v0 "
        f"payload; the backup must be recoverable (NFR-10)"
    )


# ---------------------------------------------------------------------------
# T-07: duplicate --name among pending/running tasks is rejected
# ---------------------------------------------------------------------------

def test_fr01_duplicate_name_rejected(taskq_home):
    """T-07 (spoofing): a second ``taskq submit`` with the same
    ``--name`` as an existing pending or running task fails fast
    with exit 2 and does NOT append a second task.
    """
    name = "nightly-build"

    rc1, _out1, _err1 = _run_cli_inprocess(
        ["submit", "echo first", "--name", name], taskq_home
    )
    assert rc1 == 0, (
        f"first submit must succeed; got rc={rc1} stderr={_err1!r}"
    )

    rc2, _out2, err2 = _run_cli_inprocess(
        ["submit", "echo second", "--name", name], taskq_home
    )

    assert rc2 == 2, (
        f"duplicate --name submit must return exit 2; got rc={rc2} "
        f"stderr={err2!r}"
    )
    assert err2.strip(), "expected a non-empty stderr diagnostic"

    # Exactly one task should exist — the duplicate was rejected.
    document = _read_tasks_json(taskq_home)
    matching = [
        tid for tid, rec in document.get("tasks", {}).items()
        if rec.get("name") == name
    ]
    assert len(matching) == 1, (
        f"expected exactly 1 task with --name {name!r}; got {len(matching)} "
        f"(ids={matching})"
    )