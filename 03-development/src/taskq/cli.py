"""[FR-01] taskq CLI surface.

Citations:
  03-development/tests/test_fr01.py:35 — `from taskq import cli` import
    surface (cli.main must accept an argv list and return an exit code).
  03-development/tests/test_fr01.py:83 — _run_cli subprocess entrypoint
    `python -m taskq submit ...` — dispatched via taskq.__main__.
  03-development/tests/test_fr01.py:95 — _run_cli_inprocess helper calls
    cli.main([...]) directly and inspects the return code (0 for success,
    2 for validation failure).
  03-development/tests/test_fr01.py:118 — exit code 2 on empty/whitespace
    command, non-empty stderr, no tasks.json written.
  03-development/tests/test_fr01.py:155 — exit code 2 on command length
    greater than 1000 characters.
  03-development/tests/test_fr01.py:177 — exit code 2 on any of the seven
    blacklisted characters (`;`, `|`, `&`, `$`, `>`, `<`, `` ` ``).
  03-development/tests/test_fr01.py:212 — exit code 2 on duplicate --name
    among pending/running tasks; no additional task appended.
  03-development/tests/test_fr01.py:248 — happy path: stdout is an 8-hex
    task id and tasks.json stores the pending record (status, command,
    created_at) keyed by that id.
  03-development/tests/test_fr01.py:295 — atomic write boundary: tasks.json
    is written via tmp + os.replace, leaving no orphan .tmp / ~ files.
  03-development/tests/test_fr01.py:325 — --json mode emits a single JSON
    line with `id` (8-hex) and `status: "pending"`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

MAX_COMMAND_LENGTH = 1000
BLACKLIST_CHARS = set(";|$>&<`")
ACTIVE_STATUSES = ("pending", "running")
STATUS_PENDING = "pending"
TASKS_FILENAME = "tasks.json"
EXIT_VALIDATION_ERROR = 2


def _taskq_home() -> Path:
    """[FR-01] Resolve the TASKQ_HOME directory from environment.

    Citations:
      03-development/tests/test_fr01.py:47 — `taskq_home` fixture sets
        the TASKQ_HOME env var; cli reads the same env var here.
    """
    raw = os.environ.get("TASKQ_HOME")
    if raw:
        return Path(raw)
    return Path.home() / ".taskq"


def _now_iso() -> str:
    """[FR-01] Current UTC timestamp in ISO-8601."""
    return datetime.now(timezone.utc).isoformat()


def _new_task_id() -> str:
    """[FR-01] Generate an 8-hex task id from uuid4.

    Citations:
      03-development/tests/test_fr01.py:254 — stdout must match
        ``re.fullmatch(r"[0-9a-f]{8}", printed)``.
    """
    return uuid.uuid4().hex[:8]


def _atomic_write_json(path: Path, data: dict) -> None:
    """[FR-01] Atomically persist `data` as JSON at `path`.

    Citations:
      03-development/tests/test_fr01.py:295 — NFR-03 reliability:
        tmp + os.replace must leave tasks.json valid and clean.
    """
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
            tmp.unlink()


def _load_store(home: Path) -> dict:
    """[FR-01] Read tasks.json from `home`, returning the v1 skeleton if absent.

    Citations:
      03-development/tests/test_fr01.py:248 — "tasks.json must contain a
        'tasks' key" — root dict shape is {version, tasks}.
    """
    tasks_file = home / TASKS_FILENAME
    if not tasks_file.exists():
        return {"version": 1, "tasks": {}}
    return json.loads(tasks_file.read_text(encoding="utf-8"))


def _validate_command(command: str) -> str | None:
    """[FR-01] Validate a command string against FR-01 rules.

    Returns ``None`` when the command is acceptable, otherwise an
    error message describing the rejection.

    Citations:
      03-development/tests/test_fr01.py:118 — empty / all-whitespace rejected.
      03-development/tests/test_fr01.py:155 — length > 1000 rejected.
      03-development/tests/test_fr01.py:177 — any of ;|&$><` rejected.
    """
    if not command or not command.strip():
        return "command must not be empty or whitespace"
    if len(command) > MAX_COMMAND_LENGTH:
        return (
            f"command length {len(command)} exceeds maximum "
            f"{MAX_COMMAND_LENGTH} characters"
        )
    for ch in BLACKLIST_CHARS:
        if ch in command:
            return f"command contains forbidden character {ch!r}"
    return None


def _name_is_taken(store: dict, name: str) -> bool:
    """[FR-01] Return True if `name` is already used by a pending/running task."""
    for record in store.get("tasks", {}).values():
        if record.get("status") in ACTIVE_STATUSES:
            if record.get("name") == name:
                return True
    return False


def _submit_error(message: str) -> int:
    """[FR-01] Emit a ``submit:``-prefixed diagnostic to stderr and return
    the validation-exit code.
    """
    print(f"submit: {message}", file=sys.stderr)
    return EXIT_VALIDATION_ERROR


def _cmd_submit(args: argparse.Namespace) -> int:
    """[FR-01] Handler for `taskq submit`."""
    err = _validate_command(args.command)
    if err is not None:
        return _submit_error(err)

    home = _taskq_home()
    home.mkdir(parents=True, exist_ok=True)
    store = _load_store(home)

    if args.name is not None and _name_is_taken(store, args.name):
        return _submit_error(
            f"name {args.name!r} is already used by a pending/running task"
        )

    task_id = _new_task_id()
    record = {
        "status": STATUS_PENDING,
        "command": args.command,
        "created_at": _now_iso(),
    }
    if args.name is not None:
        record["name"] = args.name
    store["tasks"][task_id] = record

    _atomic_write_json(home / TASKS_FILENAME, store)

    if args.json:
        print(json.dumps({"id": task_id, "status": STATUS_PENDING}))
    else:
        print(task_id)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    """[FR-01] Construct the top-level argparse parser."""
    parser = argparse.ArgumentParser(
        prog="taskq",
        description="taskq — task-queue CLI.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    submit = subparsers.add_parser(
        "submit",
        help="submit a new task to the queue",
    )
    submit.add_argument(
        "command",
        help="the command string to enqueue",
    )
    submit.add_argument(
        "--name",
        default=None,
        help="optional task name (must be unique among pending/running tasks)",
    )
    submit.add_argument(
        "--json",
        action="store_true",
        help="emit a single JSON object on stdout instead of a bare id",
    )
    submit.set_defaults(handler=_cmd_submit)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """[FR-01] CLI entry point.

    Args:
        argv: Argument list (excluding program name). When ``None``,
            ``sys.argv[1:]`` is used. Returns a process exit code.

    Citations:
      03-development/tests/test_fr01.py:95 — _run_cli_inprocess calls
        ``cli.main(list(args))`` and inspects the returned exit code.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    # subparsers(required=True) guarantees handler is set.
    return args.handler(args)
