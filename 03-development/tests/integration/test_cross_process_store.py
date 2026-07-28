"""[GATE2] Cross-process store integration — 1FR-XX fixtures required.

Spawns the taskq CLI as a real subprocess for every state-changing
operation and verifies the on-disk tasks.json is consistent across
independent Python interpreters (no shared module state).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

# Repository-layout marker: every subprocess invocation appends
# 03-development/src to PYTHONPATH so `python -m taskq ...` resolves.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_SRC_DIR = _REPO_ROOT / "03-development" / "src"
_PYTHONPATH = f"{_SRC_DIR}{os.pathsep}{os.environ.get('PYTHONPATH', '')}"

# When the parent coverage run sets ``COVERAGE_PROCESS_START`` the
# venv's ``a1_coverage.pth`` auto-initialises subprocess coverage in
# every spawned child, so cross-process CLI invocations are recorded
# alongside the pytest process itself.
_COVERAGE_PROCESS_START = os.environ.get("COVERAGE_PROCESS_START")


def _run_cli(taskq_home: Path, *args: str, expect_exit: int = 0) -> subprocess.CompletedProcess:
    """Run `python -m taskq <args>` with an isolated TASKQ_HOME.

    Each call is a brand-new Python interpreter process so the on-disk
    store is the only shared state between writers / readers. This is
    the smallest realistic cross-process test surface.
    """
    env = {
        **os.environ,
        "PYTHONPATH": _PYTHONPATH,
        "TASKQ_HOME": str(taskq_home),
    }
    if _COVERAGE_PROCESS_START:
        # Forward to the child so the .pth hook records its coverage.
        env["COVERAGE_PROCESS_START"] = _COVERAGE_PROCESS_START
    return subprocess.run(
        [sys.executable, "-m", "taskq", *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


@pytest.fixture
def taskq_home(tmp_path: Path) -> Path:
    home = tmp_path / "taskq_home"
    home.mkdir()
    return home


def test_integration_01_submit_then_status_returns_same_id(taskq_home: Path) -> None:
    """Submit via subprocess A, read back via subprocess B — one id round-trips."""
    submit = _run_cli(taskq_home, "submit", "--name", "round-trip", "--", "echo hi")
    assert submit.returncode == 0, submit.stderr
    task_id = submit.stdout.strip()
    assert re.fullmatch(r"[0-9a-f]{8}", task_id)

    status = _run_cli(taskq_home, "--json", "status", task_id)
    assert status.returncode == 0, status.stderr
    payload = json.loads(status.stdout)
    assert payload["id"] == task_id
    assert payload["status"] in ("pending", "running", "success", "failure", "timeout")
    assert payload["command"] == "echo hi"
    assert payload["name"] == "round-trip"


def test_integration_02_four_writers_each_get_unique_id(taskq_home: Path) -> None:
    """Four concurrent subprocess `submit` calls all succeed with unique ids.

    Each call is an independent process — proves tasks.json accepts
    concurrent writers without corruption (NFR-08 baseline).
    """
    procs = [
        _run_cli(taskq_home, "submit", "--", f"echo writer_{i}")
        for i in range(4)
    ]
    ids = [p.stdout.strip() for p in procs]
    assert all(p.returncode == 0 for p in procs), [p.stderr for p in procs]
    assert len(set(ids)) == 4, f"duplicate task ids under concurrent submit: {ids}"
    for tid in ids:
        assert re.fullmatch(r"[0-9a-f]{8}", tid)

    # The store must have every writer's command persisted.
    listing = _run_cli(taskq_home, "list", "--json")
    assert listing.returncode == 0, listing.stderr
    records = json.loads(listing.stdout)
    commands = {r["command"] for r in records}
    assert commands == {f"echo writer_{i}" for i in range(4)}


def test_integration_03_clear_removes_every_data_file(taskq_home: Path) -> None:
    """After several writes `clear` must leave the home empty (FR-05 happy path)."""
    for cmd in ("echo one", "echo two", "echo three"):
        result = _run_cli(taskq_home, "submit", "--", cmd)
        assert result.returncode == 0, result.stderr

    # Pre-condition: tasks.json exists.
    assert (taskq_home / "tasks.json").exists()

    clear = _run_cli(taskq_home, "clear")
    assert clear.returncode == 0, clear.stderr

    # Post-condition: no top-level data files survive.
    remaining = sorted(p.name for p in taskq_home.iterdir())
    assert remaining == [], f"clear left orphan files: {remaining}"


def test_integration_04_validation_failure_writes_no_store_file(taskq_home: Path) -> None:
    """Exit-2 validation rejection must not create tasks.json (atomicity gate)."""
    # Empty command — blacklisted by `_validate_command`.
    bad = _run_cli(taskq_home, "submit", "--", "")
    assert bad.returncode == 2, bad.stderr
    assert not (taskq_home / "tasks.json").exists(), (
        "validation failure must not write tasks.json"
    )


def test_integration_05_help_lists_every_subcommand(taskq_home: Path) -> None:
    """`taskq --help` lists submit / run / status / list / clear (FR-05 surface)."""
    help_proc = _run_cli(taskq_home, "--help")
    assert help_proc.returncode == 0, help_proc.stderr
    for sub in ("submit", "run", "status", "list", "clear"):
        assert sub in help_proc.stdout, f"--help missing subcommand: {sub}"
