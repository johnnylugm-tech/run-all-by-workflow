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
BLACKLIST_CHARS = frozenset(";|$>&<`")
ACTIVE_STATUSES = ("pending", "running")
STATUS_PENDING = "pending"

# Exit-code map per SPEC §7 (mirrored by test_fr05.py:650).
EXIT_SUCCESS = 0
EXIT_INTERNAL_ERROR = 1
EXIT_VALIDATION_ERROR = 2

DATA_FILENAMES = (
    store.TASKS_FILENAME,
    breaker.BREAKER_FILENAME,
    cache.CACHE_FILENAME,
)


# --- Diagnostics --------------------------------------------------------

def _fail(subcommand: str, message: str, exit_code: int) -> int:
    """[FR-01, FR-05] Emit a ``<subcommand>:``-prefixed stderr diagnostic.

    Every non-zero exit of the dispatch layer flows through here so the
    stderr channel stays uniform (and never empty — test_fr05.py:699).
    Returns ``exit_code`` so handlers can ``return _fail(...)`` directly.
    """
    print(f"{subcommand}: {message}", file=sys.stderr)
    return exit_code


def _invalid(subcommand: str, message: str) -> int:
    """[FR-01, FR-05] Reject bad input / an unknown id with exit code 2."""
    return _fail(subcommand, message, EXIT_VALIDATION_ERROR)


def _internal_error(subcommand: str, message: str) -> int:
    """[FR-05] Fail fast on a corrupt on-disk document with exit code 1.

    Citations:
      03-development/tests/test_fr05.py:727 — corrupt on-disk documents
        surface as exit 1 (NFR-07 fail-fast, never silent rebuild).
    """
    return _fail(subcommand, message, EXIT_INTERNAL_ERROR)


# --- Store access -------------------------------------------------------

def _load_tasks() -> dict:
    """[FR-05] Return the ``{task_id: record}`` map from ``$TASKQ_HOME``.

    Shared by ``status`` and ``list`` so both read the queue through the
    single store loader that honours the atomic-write boundary (NFR-03)
    and carries the schema ``version`` field (NFR-10).
    """
    return store.load_store(store.home()).get("tasks", {})


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
    # Scan the command (not the blacklist) so the diagnostic always names
    # the FIRST offending character rather than an arbitrary set member.
    for char in command:
        if char in BLACKLIST_CHARS:
            return f"command contains forbidden character {char!r}"
    return None


def _name_is_taken(store_document: dict, name: str) -> bool:
    """[FR-01] Return True if `name` is already used by a pending/running task."""
    return any(
        record.get("status") in ACTIVE_STATUSES and record.get("name") == name
        for record in store_document.get("tasks", {}).values()
    )


def _cmd_submit(args: argparse.Namespace) -> int:
    """[FR-01] Handler for `taskq submit`."""
    rejection = _validate_command(args.command)
    if rejection is not None:
        return _invalid("submit", rejection)

    home = store.home()
    home.mkdir(parents=True, exist_ok=True)
    store_document = store.load_store(home)

    if args.name is not None and _name_is_taken(store_document, args.name):
        return _invalid(
            "submit",
            f"name {args.name!r} is already used by a pending/running task",
        )

    task_id = _new_task_id()
    record = {
        "status": STATUS_PENDING,
        "command": args.command,
        "created_at": store.now_iso(),
    }
    if args.name is not None:
        record["name"] = args.name
    store_document["tasks"][task_id] = record

    # Persist the entire store (full rewrite) — submit is the entry point
    # so the task id is guaranteed new and there is no in-place update to
    # merge against.
    store._atomic_write_json(home / store.TASKS_FILENAME, store_document)

    if args.json:
        print(json.dumps({"id": task_id, "status": STATUS_PENDING}))
    else:
        print(task_id)
    return EXIT_SUCCESS


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
    lines += [f"{key}: {record[key]}" for key in sorted(record)]
    return "\n".join(lines)


def _emit_payload(payload, as_json: bool, text: str) -> int:
    """[FR-05] Print ``payload`` as one JSON line, or ``text`` otherwise.

    The global ``--json`` contract is a SINGLE line with no internal
    newlines (test_fr05.py:629), which ``json.dumps`` guarantees by
    default (no ``indent``). In text mode an empty rendering (e.g. an
    empty ``list``) prints nothing rather than a blank line.
    """
    if as_json:
        print(json.dumps(payload, ensure_ascii=False))
    elif text:
        print(text)
    return EXIT_SUCCESS


def _assert_documents_readable(subcommand: str) -> int | None:
    """[FR-05] Fail fast when an on-disk document is corrupt (NFR-07).

    Touches ``breaker.json`` through its loader — which refuses to
    silently rebuild an unparseable document — and converts the raised
    ``ValueError`` into exit code 1. Returns ``None`` when all documents
    are readable.

    Citations:
      03-development/tests/test_fr05.py:727 — corrupt ``breaker.json``
        surfaces as exit 1 before any payload is emitted.
    """
    try:
        breaker.CircuitBreaker(store.home()).state()
    except ValueError as exc:
        return _internal_error(subcommand, str(exc))
    return None


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
    # Check the on-disk documents FIRST so corruption outranks a lookup
    # miss: a corrupt home is exit 1, not exit 2.
    corrupt = _assert_documents_readable("status")
    if corrupt is not None:
        return corrupt

    record = _load_tasks().get(args.task_id)
    if record is None:
        return _invalid("status", f"unknown task id {args.task_id!r}")

    return _emit_payload(
        {"id": args.task_id, **record},
        args.json,
        _format_status_text(args.task_id, record),
    )


def _cmd_list(args: argparse.Namespace) -> int:
    """[FR-05] Handler for ``taskq list [--status S]``.

    Citations:
      03-development/tests/test_fr05.py:453 — ``--status S`` filters the
        enumeration; tasks whose status does not match are excluded.
      03-development/tests/test_fr05.py:475 — without a filter every
        task id is enumerated.
    """
    items = [
        {"id": task_id, **record}
        for task_id, record in _load_tasks().items()
        if args.status is None or record.get("status") == args.status
    ]
    return _emit_payload(
        items,
        args.json,
        "\n".join(item["id"] for item in items),
    )


def _cmd_clear(args: argparse.Namespace) -> int:
    """[FR-05] Handler for ``taskq clear`` — remove every data file under $TASKQ_HOME.

    Citations:
      03-development/tests/test_fr05.py:538 — all three data files
        (``tasks.json``, ``breaker.json``, ``cache.json``) are removed;
        exit 0 even when the home is already empty.
    """
    home = store.home()
    for filename in DATA_FILENAMES:
        # missing_ok keeps an already-empty home a clean success (exit 0)
        # without racing a concurrent clear between exists() and unlink().
        (home / filename).unlink(missing_ok=True)
    return EXIT_SUCCESS


def _build_parser() -> argparse.ArgumentParser:
    """[FR-01, FR-05] Construct the top-level argparse parser.

    Citations:
      03-development/tests/test_fr05.py:799 — ``--help`` must list every
        subcommand (submit / run / status / list / clear).
    """
    parser = argparse.ArgumentParser(
        prog="taskq",
        description="taskq — task-queue CLI.",
    )
    # Declared here for `--help` discoverability only: ``main`` strips
    # ``--json`` from argv before parsing so it is accepted both before
    # and after the subcommand.
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable single-line JSON output",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    def subcommand(name: str, summary: str, handler) -> argparse.ArgumentParser:
        """Register a subcommand and bind its dispatch handler."""
        sub = subparsers.add_parser(name, help=summary)
        sub.set_defaults(handler=handler)
        return sub

    submit = subcommand("submit", "submit a new task to the queue", _cmd_submit)
    submit.add_argument(
        "command",
        help="the command string to enqueue",
    )
    submit.add_argument(
        "--name",
        default=None,
        help="optional task name (must be unique among pending/running tasks)",
    )

    run = subcommand("run", "execute pending task(s)", _cmd_run)
    target = run.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "task_id",
        nargs="?",
        default=None,
        help="run a single task by its 8-hex id",
    )
    target.add_argument(
        "--all",
        action="store_true",
        help="run every pending task concurrently",
    )
    run.add_argument(
        "--cached",
        action="store_true",
        help="consult the TTL cache before executing (FR-04)",
    )

    status = subcommand("status", "print the full record of a task", _cmd_status)
    status.add_argument(
        "task_id",
        help="the 8-hex task id to look up",
    )

    list_cmd = subcommand(
        "list", "list tasks (optionally filtered by status)", _cmd_list
    )
    list_cmd.add_argument(
        "--status",
        default=None,
        help="only list tasks whose status matches",
    )

    subcommand("clear", "remove every data file under $TASKQ_HOME", _cmd_clear)

    return parser


def _strip_global_json(argv: Sequence[str]) -> tuple[list[str], bool]:
    """[FR-05] Remove every ``--json`` token from ``argv``; report its presence.

    ``--json`` is a GLOBAL flag (SAD §3.1), so it must be honoured both
    before and after the subcommand: existing FR-01 callers write
    ``taskq submit ... --json`` while the FR-05 surface writes
    ``taskq --json status <id>``. Stripping the token up front means no
    subparser has to redeclare it just to accept it in trailing position.
    """
    return [token for token in argv if token != "--json"], "--json" in argv


def main(argv: Sequence[str] | None = None) -> int:
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
    tokens = list(sys.argv[1:] if argv is None else argv)
    tokens, json_requested = _strip_global_json(tokens)

    args = _build_parser().parse_args(tokens)
    args.json = json_requested
    # subparsers(required=True) guarantees handler is set.
    return args.handler(args)