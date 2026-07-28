"""[FR-01, FR-05] taskq CLI surface — dispatch hub for all subcommands.

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

[FR-02, FR-04] taskq run subcommand:
  03-development/tests/test_fr02.py:267 — `cli.main(["run", task_id])`
    dispatches to `executor.run(task_id)`; a single-task timeout returns 4.
  03-development/tests/test_fr02.py:298 — `python -m taskq run <id>`
    subprocess surface; exit code 4 on ``TASKQ_TASK_TIMEOUT``.
  03-development/tests/test_fr02.py:439 — `cli.main(["run", "--all"])`
    dispatches to `executor.run_all()`; concurrent fan-out exit 0 (or 3
    for breaker-OPEN sentinel reserved by FR-03).
  03-development/tests/test_fr02.py:466 — `python -m taskq run --all`
    subprocess surface; tasks.json must remain valid JSON.

[FR-05] status / list / clear subcommands and global --json:
  03-development/tests/test_fr05.py:369 — `taskq status <id>` prints the
    full task record on stdout; exit 0 on hit.
  03-development/tests/test_fr05.py:453 — `taskq list --status S` filters
    the task enumeration by status.
  03-development/tests/test_fr05.py:538 — `taskq clear` removes every
    data file under $TASKQ_HOME; exit 0 even on an empty home.
  03-development/tests/test_fr05.py:604 — `--json` is a GLOBAL flag; when
    supplied it converts status (and every other payload-emitting
    subcommand) to a single-line JSON object on stdout.
  03-development/tests/test_fr05.py:638 — exit-code map per SPEC §7:
    0 success, 2 validation + unknown id, 3 breaker open, 4 single-task
    timeout, 1 internal / corrupt-on-disk-document.
  03-development/tests/test_fr05.py:773 — `python -m taskq --help` exits 0
    and lists every subcommand (submit / run / status / list / clear).
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from typing import Sequence

from taskq import breaker, cache, executor, store  # noqa: F401

MAX_COMMAND_LENGTH = 1000
BLACKLIST_CHARS = set(";|$>&<`")
ACTIVE_STATUSES = ("pending", "running")
STATUS_PENDING = "pending"
EXIT_VALIDATION_ERROR = 2
EXIT_INTERNAL_ERROR = 1

DATA_FILENAMES = (
    store.TASKS_FILENAME,
    breaker.BREAKER_FILENAME,
    cache.CACHE_FILENAME,
)


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


def _internal_error(message: str) -> int:
    """[FR-05] Emit a stderr diagnostic and return exit code 1.

    Citations:
      03-development/tests/test_fr05.py:727 — corrupt on-disk documents
        surface as exit 1 (NFR-07 fail-fast, never silent rebuild).
    """
    print(message, file=sys.stderr)
    return EXIT_INTERNAL_ERROR


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
    """[FR-02, FR-04] Dispatch ``taskq run`` to the executor.

    Citations:
      03-development/tests/test_fr02.py:267 — single-task timeout surfaces
        as OS exit 4 via ``executor.run``'s return value.
      03-development/tests/test_fr02.py:439 — ``--all`` dispatches to
        ``executor.run_all`` which fans out under the shared store Lock.
      03-development/tests/test_fr05.py:308 — ``--cached`` flag is wired
        through to ``executor.run(task_id, use_cache=True)``.
    """
    if args.all:
        return executor.run_all()
    return executor.run(args.task_id, use_cache=args.cached)


def _format_status_text(task_id: str, record: dict) -> str:
    """[FR-05] Render the task record as ``key: value`` lines for stdout.

    Citations:
      03-development/tests/test_fr05.py:375 — non-JSON status output must
        surface every persisted field as a substring of stdout.
    """
    lines = [f"id: {task_id}"]
    for key in sorted(record.keys()):
        lines.append(f"{key}: {record[key]}")
    return "\n".join(lines)


def _cmd_status(args: argparse.Namespace) -> int:
    """[FR-05] Handler for ``taskq status <id>``.

    Citations:
      03-development/tests/test_fr05.py:369 — ``status <id>`` prints the
        full record on stdout; exit 0 on hit.
      03-development/tests/test_fr05.py:604 — ``--json`` emits the record
        as a single-line JSON object.
      03-development/tests/test_fr05.py:727 — corrupt ``breaker.json``
        surfaces as exit 1 (NFR-07 fail-fast).
    """
    home = store.home()
    store_data = store.load_store(home)
    tasks = store_data.get("tasks", {})

    # Touch the breaker FIRST so a corrupt document surfaces as exit 1
    # (NFR-07 fail-fast; never silently rebuild on-disk documents).
    try:
        breaker.CircuitBreaker(home).state()
    except ValueError as exc:
        return _internal_error(f"status: {exc}")

    record = tasks.get(args.task_id)
    if record is None:
        return _submit_error(f"unknown task id {args.task_id!r}")

    payload = {"id": args.task_id, **record}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(_format_status_text(args.task_id, record))
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    """[FR-05] Handler for ``taskq list [--status S]``.

    Citations:
      03-development/tests/test_fr05.py:453 — ``--status S`` filters the
        enumeration; tasks whose status does not match are excluded.
      03-development/tests/test_fr05.py:475 — without a filter every
        task id is enumerated.
    """
    home = store.home()
    store_data = store.load_store(home)
    tasks = store_data.get("tasks", {})

    wanted = args.status
    if wanted is not None:
        filtered = {
            tid: rec for tid, rec in tasks.items() if rec.get("status") == wanted
        }
    else:
        filtered = dict(tasks)

    items = [{"id": tid, **rec} for tid, rec in filtered.items()]

    if args.json:
        print(json.dumps(items, ensure_ascii=False))
    else:
        for item in items:
            print(item["id"])
    return 0


def _cmd_clear(args: argparse.Namespace) -> int:
    """[FR-05] Handler for ``taskq clear`` — remove every data file under $TASKQ_HOME.

    Citations:
      03-development/tests/test_fr05.py:538 — all three data files
        (``tasks.json``, ``breaker.json``, ``cache.json``) are removed;
        exit 0 even when the home is already empty.
    """
    home = store.home()
    for fname in DATA_FILENAMES:
        path = home / fname
        if path.exists():
            path.unlink()
    return 0


def _build_parser() -> argparse.ArgumentParser:
    """[FR-01, FR-05] Construct the top-level argparse parser."""
    parser = argparse.ArgumentParser(
        prog="taskq",
        description="taskq — task-queue CLI.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable single-line JSON output",
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
        help=argparse.SUPPRESS,
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
    run.add_argument(
        "--cached",
        action="store_true",
        help="consult the TTL cache before executing (FR-04)",
    )
    run.set_defaults(handler=_cmd_run)

    status = subparsers.add_parser(
        "status",
        help="print the full record of a task",
    )
    status.add_argument(
        "task_id",
        help="the 8-hex task id to look up",
    )
    status.set_defaults(handler=_cmd_status)

    list_cmd = subparsers.add_parser(
        "list",
        help="list tasks (optionally filtered by status)",
    )
    list_cmd.add_argument(
        "--status",
        default=None,
        help="only list tasks whose status matches",
    )
    list_cmd.set_defaults(handler=_cmd_list)

    clear = subparsers.add_parser(
        "clear",
        help="remove every data file under $TASKQ_HOME",
    )
    clear.set_defaults(handler=_cmd_clear)

    return parser


def _resolve_global_json(argv: Sequence[str]) -> tuple[list[str], bool]:
    """[FR-05] Strip ``--json`` from anywhere in ``argv`` and return its presence.

    The global ``--json`` flag is honoured before AND after the subcommand
    so existing FR-01 callers (``taskq submit ... --json``) keep working
    alongside the new FR-05 surface (``taskq --json status ...``). The
    stripped argv is then handed to ``argparse`` whose subparser-level
    ``--json`` records the same intent in the namespace.
    """
    return [a for a in argv if a != "--json"], "--json" in argv


def main(argv: Sequence[str | None] | None = None) -> int:
    """[FR-01, FR-05] CLI entry point.

    Args:
        argv: Argument list (excluding program name). When ``None``,
            ``sys.argv[1:]`` is used. Returns a process exit code.

    Citations:
      03-development/tests/test_fr01.py:95 — _run_cli_inprocess calls
        ``cli.main(list(args))`` and inspects the returned exit code.
      03-development/tests/test_fr05.py:114 — in-process dispatch reaches
        the same handlers as the subprocess entry point.
      03-development/tests/test_fr05.py:727 — ValueError from a corrupt
        on-disk document is converted to exit 1 with a stderr diagnostic.
    """
    if argv is None:
        argv = list(sys.argv[1:])
    else:
        argv = list(argv)

    parser = _build_parser()
    cleaned, has_json = _resolve_global_json(argv)
    args = parser.parse_args(cleaned) if cleaned else parser.parse_args([])
    # The subparser-level --json (preserved for FR-01) and the global flag
    # must agree; OR them so either path lights up JSON output.
    args.json = bool(getattr(args, "json", False)) or has_json
    # subparsers(required=True) guarantees handler is set.
    return args.handler(args)