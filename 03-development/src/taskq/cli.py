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

[FR-02] taskq run subcommand:
  03-development/tests/test_fr02.py:267 — `cli.main(["run", task_id])`
    dispatches to `executor.run(task_id)`; a single-task timeout returns 4.
  03-development/tests/test_fr02.py:298 — `python -m taskq run <id>`
    subprocess surface; exit code 4 on ``TASKQ_TASK_TIMEOUT``.
  03-development/tests/test_fr02.py:439 — `cli.main(["run", "--all"])`
    dispatches to `executor.run_all()`; concurrent fan-out exit 0 (or 3
    for breaker-OPEN sentinel reserved by FR-03).
  03-development/tests/test_fr02.py:466 — `python -m taskq run --all`
    subprocess surface; tasks.json must remain valid JSON.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from typing import Sequence

from taskq import executor, store  # noqa: F401  (executor used by _cmd_run dispatch)

MAX_COMMAND_LENGTH = 1000
BLACKLIST_CHARS = set(";|$>&<`")
ACTIVE_STATUSES = ("pending", "running")
STATUS_PENDING = "pending"
EXIT_VALIDATION_ERROR = 2


def _new_task_id() -> str:
    """[FR-01] Generate an 8-hex task id from uuid4.

    Citations:
      03-development/tests/test_fr01.py:254 — stdout must match
        ``re.fullmatch(r"[0-9a-f]{8}", printed)``.
    """
    return uuid.uuid4().hex[:8]


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


def _name_is_taken(store_data: dict, name: str) -> bool:
    """[FR-01] Return True if `name` is already used by a pending/running task."""
    for record in store_data.get("tasks", {}).values():
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

    home = store.home()
    home.mkdir(parents=True, exist_ok=True)
    store_data = store.load_store(home)

    if args.name is not None and _name_is_taken(store_data, args.name):
        return _submit_error(
            f"name {args.name!r} is already used by a pending/running task"
        )

    task_id = _new_task_id()
    record = {
        "status": STATUS_PENDING,
        "command": args.command,
        "created_at": store.now_iso(),
    }
    if args.name is not None:
        record["name"] = args.name
    store_data["tasks"][task_id] = record

    # Persist the entire store (full rewrite) — submit is the entry point
    # so the task id is guaranteed new and there is no in-place update to
    # merge against.
    store._atomic_write_json(home / store.TASKS_FILENAME, store_data)

    if args.json:
        print(json.dumps({"id": task_id, "status": STATUS_PENDING}))
    else:
        print(task_id)
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    """[FR-02] Dispatch ``taskq run`` to the executor.

    Citations:
      03-development/tests/test_fr02.py:267 — single-task timeout surfaces
        as OS exit 4 via ``executor.run``'s return value.
      03-development/tests/test_fr02.py:439 — ``--all`` dispatches to
        ``executor.run_all`` which fans out under the shared store Lock.
    """
    if args.all:
        return executor.run_all()
    return executor.run(args.task_id)


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

    run = subparsers.add_parser(
        "run",
        help="execute pending task(s)",
    )
    run_group = run.add_mutually_exclusive_group(required=True)
    run_group.add_argument(
        "task_id",
        nargs="?",
        default=None,
        help="run a single task by its 8-hex id",
    )
    run_group.add_argument(
        "--all",
        action="store_true",
        help="run every pending task concurrently",
    )
    run.set_defaults(handler=_cmd_run)
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