"""[bug-hunt] TDD-RED repro tests for confirmed high-severity findings.

These tests RED-fail on the current code to prove the bugs are real,
and turn GREEN once the corresponding source fix is applied.

Threat-model mapping:
  test_bh_f1_concurrent_submit_unique_names       → T-07 (spoofing mitigation gap)
  test_bh_f2_concurrent_submits_no_data_loss     → T-05 (corruption/loss)
  test_bh_f3_subprocess_output_redacted           → T-04 (information disclosure)
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SRC_DIR = _REPO_ROOT / "03-development" / "src"
_PYTHONPATH = f"{_SRC_DIR}{os.pathsep}{os.environ.get('PYTHONPATH', '')}"
_COVERAGE_PROCESS_START = os.environ.get("COVERAGE_PROCESS_START")


def _run_cli(taskq_home: Path, *args: str) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "PYTHONPATH": _PYTHONPATH,
        "TASKQ_HOME": str(taskq_home),
    }
    if _COVERAGE_PROCESS_START:
        env["COVERAGE_PROCESS_START"] = _COVERAGE_PROCESS_START
    return subprocess.run(
        [sys.executable, "-m", "taskq", *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        # close_fds=True prevents pytest / coverage from inheriting an open
        # flock FD into the child — without it the child re-enters the
        # same lock and deadlocks waiting for itself.
        close_fds=True,
    )


@pytest.fixture
def taskq_home(tmp_path: Path) -> Path:
    home = tmp_path / "taskq_home"
    home.mkdir()
    return home


# ---------------------------------------------------------------------------
# F1 — cli._cmd_submit must serialise under STORE_LOCK (T-07 mitigation gap)
# ---------------------------------------------------------------------------

def test_bh_f1_concurrent_submit_unique_names(taskq_home: Path) -> None:
    """10 concurrent subprocess submits with the SAME --name must produce
    EXACTLY 1 active pending task (the rest must be rejected).

    Failure scenario pre-fix: the check `_name_is_taken` and the subsequent
    `_atomic_write_json` are NOT under STORE_LOCK, so multiple processes
    can each load an empty store, each pass the name check, and each write
    their own task record → multiple concurrent active records with the
    same --name (state shadowing).
    """
    name = "shadow-target"

    def one_submit() -> subprocess.CompletedProcess:
        return _run_cli(taskq_home, "submit", "--name", name, "--", "echo hi")

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(lambda _: one_submit(), range(10)))

    accepts = sum(1 for r in results if r.returncode == 0)
    rejects = sum(1 for r in results if r.returncode == 2)
    # The post-fix contract: exactly one accept, the rest are rejected.
    assert accepts == 1, (
        f"expected exactly 1 accepted submit (the others must be rejected "
        f"as duplicate-name), got accepts={accepts} rejects={rejects}; "
        f"stderr={[r.stderr for r in results if r.returncode != 0]}"
    )

    # And exactly one task with that name is persisted.
    assert (taskq_home / "tasks.json").exists(), (
        "tasks.json must exist after a successful submit"
    )
    data = json.loads((taskq_home / "tasks.json").read_text())
    shadowed = [tid for tid, rec in data["tasks"].items() if rec.get("name") == name]
    assert len(shadowed) == 1, (
        f"exactly one task may carry --name={name!r}; got {len(shadowed)}: "
        f"{shadowed}"
    )


# ---------------------------------------------------------------------------
# F2 — store._atomic_write_json + concurrent processes must not lose data
#      (T-05 declared corruption attack vector)
# ---------------------------------------------------------------------------

def test_bh_f2_concurrent_submits_no_data_loss(taskq_home: Path) -> None:
    """N concurrent subprocess submits must persist ALL N records.

    Failure scenario pre-fix: store._atomic_write_json writes a tmp file
    named `.{name}.{8-hex-uuid}.tmp`, then `os.replace`s, then in `finally`
    unconditionally `tmp.unlink()`s. Under cross-process races two writers
    can pick the SAME 8-hex tmp suffix and one's `finally.unlink` removes
    the other's still-pending tmp → ENOENT on `os.replace`, file lost,
    tasks.json may end up deleted entirely.
    """
    n = 20

    def one_submit(i: int) -> subprocess.CompletedProcess:
        return _run_cli(taskq_home, "submit", "--", f"echo writer_{i}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=n) as pool:
        results = list(pool.map(one_submit, range(n)))

    accepts = sum(1 for r in results if r.returncode == 0)
    assert accepts == n, (
        f"every concurrent submit must succeed; got accepts={accepts}/{n}; "
        f"first failure stderr: {[r.stderr for r in results if r.returncode != 0][:1]}"
    )

    # tasks.json must exist and parse as valid JSON.
    assert (taskq_home / "tasks.json").exists(), (
        "tasks.json must persist after concurrent writes; the tmp-unlink "
        "race deleted it"
    )
    data = json.loads((taskq_home / "tasks.json").read_text())
    commands = {rec.get("command") for rec in data["tasks"].values()}
    expected = {f"echo writer_{i}" for i in range(n)}
    assert commands == expected, (
        f"every writer's command must be persisted; missing="
        f"{expected - commands}; extra={commands - expected}"
    )


# ---------------------------------------------------------------------------
# F3 — executor must redact secrets from persisted stdout_tail/stderr_tail
#      (T-04 declared information-disclosure attack vector)
# ---------------------------------------------------------------------------

_REDACT_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9]{16,}"),           # OpenAI-style API keys
    re.compile(r"token=[A-Za-z0-9]{16,}"),         # generic token=value
)


def test_bh_f3_subprocess_output_redacted(taskq_home: Path) -> None:
    """Subprocess output that contains a secret-style token must NOT be
    persisted verbatim to tasks.json (or cache.json).

    Failure scenario pre-fix: executor._execute returns
    ``_tail(result.stdout)`` verbatim and cache.put persists the same
    dict. A user who submits a command whose output contains an API key
    or token has that secret durably written to disk under
    $TASKQ_HOME/{tasks.json,cache.json} — recoverable by any process that
    reads those files (information disclosure, T-04).
    """
    # Build a command that prints a fake API key. `;` is blacklisted so
    # use a python one-liner that emits the secret to stdout.
    # NOTE: kept lower-entropy / non-credential-shaped so secrets_scanning
    # tools (gitleaks) do not flag it as a real key — the redaction
    # regex above only needs `sk-` + 16+ alphanum to match.
    secret = "sk-test-redact-dummy-value-aaaaaaaaaa"
    cmd = f'python3 -c "print(\'{secret}\')"'

    submit = _run_cli(taskq_home, "submit", "--", cmd)
    assert submit.returncode == 0, submit.stderr
    task_id = submit.stdout.strip()

    run = _run_cli(taskq_home, "run", task_id)
    assert run.returncode == 0, run.stderr

    # Read tasks.json — secret must not appear verbatim.
    data = json.loads((taskq_home / "tasks.json").read_text())
    record = data["tasks"][task_id]
    persisted = " ".join([
        str(record.get("stdout_tail", "")),
        str(record.get("stderr_tail", "")),
    ])
    for pat in _REDACT_PATTERNS:
        assert not pat.search(persisted), (
            f"persisted output must not contain secret matching {pat.pattern}; "
            f"got persisted={persisted!r}"
        )

    # Read cache.json — same redaction must apply.
    cache_path = taskq_home / "cache.json"
    if cache_path.exists():
        cache = json.loads(cache_path.read_text())
        for entry in cache.get("entries", {}).values():
            cached = " ".join([
                str(entry.get("result", {}).get("stdout_tail", "")),
                str(entry.get("result", {}).get("stderr_tail", "")),
            ])
            for pat in _REDACT_PATTERNS:
                assert not pat.search(cached), (
                    f"cache.json must not contain secret matching {pat.pattern}; "
                    f"got cached={cached!r}"
                )