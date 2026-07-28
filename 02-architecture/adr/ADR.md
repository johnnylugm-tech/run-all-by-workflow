# Architecture Decision Records (ADR) — taskq

> Source of architectural decisions for the `taskq` local task-queue CLI.
> Each entry below is binding for implementation; deviations require a
> new ADR that supersedes the old one.
> Authoritative spec: `SPEC.md` v4.0.0; architecture: `SAD.md` v1.0.0.
> Project date: 2026-07-28.

---

## ADR-001: Python 3.11 standard library only (zero runtime dependencies)

### Status
Accepted — 2026-07-28.

### Context
`taskq` is a local CLI for submitting, running, retrying, caching, and
inspecting shell commands. The runtime is `python -m taskq <subcommand>`
on developer and CI machines. The team needs (a) the smallest possible
install footprint, (b) zero supply-chain surface, (c) portability across
Linux, macOS, and Windows. The SPEC explicitly constrains the runtime
package set to the Python standard library.

### Decision
Implement `taskq` **entirely in Python 3.11** using only the standard
library. Verified runtime: `Python 3.11.15` (read from `.venv/bin/python
--version` during this audit). All subprocess, file-locking, JSON,
shlex, dataclass, and threading needs are covered by `subprocess`,
`fcntl`/`msvcrt`, `json`, `shlex`, `dataclasses`, `concurrent.futures`,
`threading`, `pathlib`, `os`, `sys`, `time`, `datetime`, `hashlib`,
`re`, `uuid`, `argparse`, and `logging`.

### Consequences
- Positive: No `pip install`; no lockfile; no transitive CVE surface;
  no version-conflict headaches; same code runs on Linux, macOS, Windows.
- Positive: Aligns with `SPEC.md` §10 framework constraint
  `no_circular_dependencies` and the harness-methodology "minimal
  surface" principle.
- Negative: Cannot leverage third-party libraries (e.g. `pydantic`,
  `click`, `rich`) without breaking the contract; some ergonomic
  functionality must be hand-rolled (e.g. ANSI colors, table rendering).
- Negative: Python 3.11 is the floor; users on 3.10 or earlier get
  a clear `SyntaxError`/runtime error at first import.

### Alternatives considered
- **Add `click` for the CLI**: rejected — `argparse` is sufficient for
  5 subcommands and is in stdlib; the dependency cost (transitive deps
  + supply-chain) outweighs the ergonomic win.
- **Add `pydantic` for model validation**: rejected — `dataclasses`
  plus explicit `__post_init__` checks are adequate for the 4-model
  surface (`Task`, `TaskStatus`, `BreakerState`, `CacheEntry`).
- **Target Python 3.10 for broader compatibility**: rejected — the
  team's baseline env is 3.11 (`.venv` pins it), and 3.11 features
  (`match`/`case`, `tomllib`, `ExceptionGroup`) are available when
  needed.

---

## ADR-002: Single-process local CLI with layered modules

### Status
Accepted — 2026-07-28.

### Context
`taskq` has five user-facing subcommands (`submit`, `run`, `run --all`,
`status`, `list`, `clear`) and four internal concerns (subprocess
execution, caching, circuit-breaking, JSON persistence). Without a
disciplined layering rule, modules grow to know about each other
through ad-hoc imports, which `harness/CLAUDE.md` forbids via
`no_circular_dependencies`.

### Decision
Adopt a **strict four-layer topology** with one allowed dependency
direction:

```
entry         → execution, persistence, config
execution     → persistence, config
persistence   → config
config        → (stdlib only)
```

- **Entry**: `taskq.__main__`, `taskq.cli`.
- **Execution**: `taskq.executor`, `taskq.breaker`, `taskq.cache`.
- **Persistence**: `taskq.store`, `taskq.models`.
- **Config**: `taskq.config`.

SAD §3.4 records the exact edges; the SAB block in SAD §5 encodes the
same topology as `allowed_dependencies`, which the
`sab_parser.py:render_canonical_sab_template()` validator enforces.

### Consequences
- Positive: Static cycle check is trivial; `tests/test_layers.py`
  can use `importlib` to assert forbidden edges.
- Positive: Module per-file count stays ≤ 9, well under the 15 cap.
- Negative: Layered code requires more `__init__.py` re-exports and
  clearer docstrings; a small amount of boilerplate.
- Negative: Cross-cutting features (e.g. telemetry) require a new
  layer or an explicit out-of-band channel — they cannot silently
  reach across layers.

### Alternatives considered
- **Flat package with no layering**: rejected — `harness/CLAUDE.md`
  forbids circular deps; without a layered rule, "where do I import
  from?" becomes a per-feature negotiation.
- **Hexagonal/ports-and-adapters**: rejected — over-engineered for a
  9-module CLI; the layered model captures all the real constraints
  with less ceremony.

---

## ADR-003: Single persistent-store boundary (`taskq.store`)

### Status
Accepted — 2026-07-28.

### Context
Three JSON files (`tasks.json`, `breaker.json`, `cache.json`) must be
written atomically, locked cross-process, schema-migrated on read,
and exposed to fault-injection tests. Spreading these concerns across
modules would mean every reader/writer replicates the same boilerplate
and risks drift.

### Decision
All I/O on `$TASKQ_HOME/*.json` flows through `taskq.store`. The
single public surface is:

```
taskq.store.atomic_write(path, payload) -> None
taskq.store.read(path) -> dict
taskq.store.under_lock(fn) -> T   # context manager
```

`atomic_write` is the **only** code path that owns the
`tmp + os.replace` pattern and the `flock` acquisition. Call sites
import `store` and call it; they never reach for `os.replace` or
`fcntl.flock` directly.

### Consequences
- Positive: One bound on where atomic-write and locking live → easy
  to audit, easy to test, easy to fault-inject.
- Positive: A single change to the write protocol (e.g. swap atomic
  primitive) propagates to all three files.
- Negative: Callers must respect the `under_lock` discipline for
  read-modify-write sequences; a forgotten lock is a latent bug.
- Negative: All three files share one lock-primitive path; if a
  future change needs per-file lock policies, it must be carefully
  layered on top of `store` rather than bypassed.

### Alternatives considered
- **Per-module I/O** (each of `breaker`, `cache`, `executor` owns its
  JSON file): rejected — duplicates the atomic-write + lock code
  three times and risks divergence.
- **SQLite as the store**: rejected — adds a non-stdlib dependency
  and changes the failure mode (WAL files, journaling) in ways
  that don't pay back for the 3-file, 1000-row scale.

---

## ADR-004: Atomic write via `tmp + os.replace`

### Status
Accepted — 2026-07-28.

### Context
A crash mid-write must never leave a JSON file truncated or half-
written. POSIX guarantees `os.replace` is atomic; the companion
`tmp + os.replace` pattern is the canonical safe-write idiom.

### Decision
`taskq.store.atomic_write` writes `<file>.tmp` in the same directory,
flushes the file descriptor, then calls `os.replace(tmp, target)`. The
directory is the same as the target so `os.replace` is a single
inode rename on the same filesystem (atomic on POSIX and on NTFS).
The file is opened with `O_CREAT | O_WRONLY | O_TRUNC` and `f.flush()`
+ `os.fsync()` is called before `os.replace`.

### Consequences
- Positive: Crash at any point either leaves the original file
  intact (write in progress) or the new file fully in place
  (replace done) — never a torn write.
- Positive: Zero extra dependencies; OS-level guarantee.
- Negative: Requires write permission on the parent directory.
- Negative: On network filesystems (NFS without `rename` atomicity),
  atomicity may degrade; the design mitigates this with a
  `WARNING`-logged detection (NFR-08).

### Alternatives considered
- **Write in place + journal/recover file**: rejected — more
  complexity, two files to keep in sync, and the user-visible
  semantics of "did the write succeed?" are harder to reason about.
- **`shutil.copy` + rename**: rejected — `os.replace` is the
  documented POSIX-portable atomic-rename primitive; `shutil` adds
  no benefit here.

---

## ADR-005: Cross-process safety via `fcntl.flock` (POSIX) / `msvcrt.locking` (Windows)

### Status
Accepted — 2026-07-28.

### Context
Two concurrent `python -m taskq` invocations could both read
`breaker.json` in CLOSED state, both decide to run a task, both write
back OPEN, and double-submit a trial request. Per NFR-08, the system
must enforce cross-process serialization on shared JSON files.

### Decision
`taskq.store` acquires a `flock` (POSIX) or `msvcrt.locking` (Windows)
shared/exclusive lock around every read-modify-write on the three
JSON files. The lock is **per-file** and **exclusive** for writes; the
in-process `threading.Lock` provides per-process serialization; the
`flock` provides cross-process serialization. On network FS that
does not support `flock`, the system degrades: a `WARNING` is logged,
`flock` is skipped, but atomic write is preserved.

### Consequences
- Positive: Two concurrent processes serialize correctly on the
  same file; no JSON corruption under contention.
- Positive: Graceful degradation on NFS/CIFS — atomic write still
  prevents torn writes even when `flock` is unavailable.
- Negative: Network FS users see a `WARNING` and rely on atomic
  rename only; malicious or buggy concurrent writers on the same
  network FS could still cause damage.
- Negative: Locking with `flock` is advisory; non-`taskq` writers
  that ignore the convention can still corrupt files.

### Alternatives considered
- **Database (SQLite) with WAL mode**: rejected — adds a non-stdlib
  dependency and changes the operational story (DB file, migrations).
- **Per-file reader/writer process (single-writer design)**: rejected
  — over-engineered for a CLI; user expectation is multiple short-
  lived invocations.

---

## ADR-006: Schema versioning with `<file>.v<n>.bak` migration backups

### Status
Accepted — 2026-07-28.

### Context
NFR-10 requires that older JSON files (v0 or any prior version) be
migrated to v1 on read, and that the original be preserved so a
fail-fast recovery is possible. Mishandling migrations could silently
overwrite user data or break state.

### Decision
Every JSON file has a top-level `version: int` field. On `read`, if
the file is missing the field or has a lower version, `store` bumps
it to the current version and writes the new file. Before overwriting,
the original is copied to `<file>.v<n>.bak` (same directory). If
migration fails for any reason, the process exits 1 (fail-fast).

### Consequences
- Positive: Real user data is never lost during a migration.
- Positive: Failure is loud, not silent — exec exits 1 with a clear
  error.
- Negative: Backup files accumulate over multiple schema bumps; an
  operator must clean them up.
- Negative: Migration logic must live somewhere; it adds a few
  hundred lines to `store`.

### Alternatives considered
- **In-place migration with no backup**: rejected — silent
  data loss on a botched migration is unacceptable.
- **External migration tool (e.g. `alembic`)**: rejected — far too
  heavy for a JSON-file store with 3 schemas.

---

## ADR-007: ThreadPoolExecutor for `run --all` with shared `store.Lock`

### Status
Accepted — 2026-07-28.

### Context
`run --all` must process N pending tasks concurrently. The number of
workers is configurable via `TASKQ_MAX_WORKERS`. Each worker writes
its task's result to `tasks.json` (via `store.update`) and may read
or write `breaker.json` and `cache.json`. Without coordination, two
workers could both pass `breaker.allow()` from HALF_OPEN state and
both issue a trial task.

### Decision
`run --all` uses `concurrent.futures.ThreadPoolExecutor(max_workers=
TASKQ_MAX_WORKERS)`. Every cross-file state mutation runs through
`store.under_lock(...)` (a process-local `threading.Lock`) which
serializes writes within the process. The `flock` in `store.read`
and `store.atomic_write` (ADR-005) provides cross-process
serialization.

### Consequences
- Positive: GIL-bound I/O work (subprocess) runs in parallel; CPU
  work is minimal and not a bottleneck.
- Positive: `store.under_lock` is the single discipline for every
  read-modify-write; reviewers can audit one place.
- Negative: A single process-local lock means across-worker
  parallelism in file I/O is limited — but the I/O is bounded
  (small JSON files), so this is fine.
- Negative: A future move to `ProcessPoolExecutor` would require
  re-engineering the lock discipline (per-process locks + IPC),
  which is a separate ADR.

### Alternatives considered
- **`multiprocessing` `Pool`**: rejected — IPC overhead and
  pickling of `Task` objects is heavier than necessary for the
  single-process model; cross-process safety is already handled by
  `flock` in `store`.
- **Sequential execution**: rejected — `run --all` is the user-
  visible scaling path; the SPEC requires concurrency.

---

## ADR-008: Circuit breaker (CLOSED / OPEN / HALF_OPEN) with `breaker.json`

### Status
Accepted — 2026-07-28.

### Context
Repeated subprocess failures (e.g. a network-dependent command that
the user keeps submitting) should not consume worker slots
indefinitely. NFR-03 mandates a circuit breaker that opens after
`TASKQ_BREAKER_THRESHOLD` consecutive failures, stays open for
`TASKQ_BREAKER_COOLDOWN` seconds, then admits one trial task in
HALF_OPEN to probe recovery.

### Decision
Implement a three-state machine in `taskq.breaker`:

```
CLOSED ──threshold failures──► OPEN
OPEN   ──cooldown elapsed──► HALF_OPEN
HALF_OPEN ──trial success──► CLOSED
HALF_OPEN ──trial failure──► OPEN
```

State is persisted to `breaker.json` (schema `{version:1, state,
failure_count, opened_at}`). Transitions are read-modify-write
under `store.under_lock` + `flock`, so two concurrent processes
cannot both transition from HALF_OPEN and both run a trial task.

### Consequences
- Positive: Able to halt a runaway failure cascade without manual
  intervention.
- Positive: Cross-process visibility — `taskq status` and a
  concurrent `taskq run` see the same state.
- Negative: HALF_OPEN admits exactly one trial; under heavy
  concurrency, the back-pressure from HALF_OPEN is by design.
- Negative: The fail-fast vs. retry tension requires the cooldown
  to be reasonable (e.g. 30s) — too short, and the breaker flutters.

### Alternatives considered
- **No breaker, just per-task retry**: rejected — does not protect
  against systematic failures (e.g. upstream service is down).
- **Rate limiter (token bucket)**: rejected — doesn't capture the
  failure-rate asymmetry the breaker provides (closed → open is
  a state, not a rate).

---

## ADR-009: Injectable `sleep` for deterministic retry tests

### Status
Accepted — 2026-07-28.

### Context
Exponential backoff between retries normally uses `time.sleep`. Tests
that exercise the retry loop would have to wait real wall-clock time
or rely on `monkeypatch` globally. The SPEC requires the retry
behavior to be unit-tested deterministically.

### Decision
`taskq.executor.run` exposes a module-level `_sleep:
Callable[[float], None]` parameter, defaulting to `time.sleep`. Tests
import the module and override `_sleep` with a fake that records
calls without sleeping. The alternative — `unittest.mock.patch` on
`time.sleep` — is heavier and less localized.

### Consequences
- Positive: Retry tests are fast and deterministic.
- Positive: The default behaviour is unchanged for production.
- Negative: A second pattern (alongside `monkeypatch`) for faking
  time — reviewers must recognize `_sleep` to read the code.
- Negative: If a future call site forgets to pass `_sleep`, it
  silently falls back to `time.sleep` — a mild test-ergonomics
  foot-gun.

### Alternatives considered
- **`unittest.mock.patch("time.sleep")` everywhere**: rejected —
  requires the test to know the internal name `time.sleep`
  *inside* `executor`; any refactor breaks tests.
- **`freezegun` library**: rejected — non-stdlib dependency,
  contradicts ADR-001.

---

## ADR-010: No `shell=True` — `shlex.split` only

### Status
Accepted — 2026-07-28.

### Context
If `subprocess.run` is called with `shell=True`, a user-supplied
command string can chain arbitrary shell syntax, even after the
FR-01 injection blacklist. This is the canonical shell-injection
sink. NFR-02 forbids `shell=True` *anywhere* in `src/taskq`.

### Decision
`taskq.executor.run` accepts the raw command string and calls
`shlex.split(command)` internally, then passes the resulting list to
`subprocess.run(..., shell=False)`. Call sites never pass
already-split `argv` arrays. The Gate 1 verification includes a
static rule that `shell=True` does not appear in `src/taskq/`.

### Consequences
- Positive: Shell metacharacters like `;`, `|`, `&`, `$`, `>`, `<`, ``
  ` `` are parsed as arguments, not interpreted by a shell.
- Positive: The grep-style static rule is cheap to maintain.
- Negative: Some legitimately shell-y user commands (e.g. pipes
  inside the command string) won't work — but the FR-01 blacklist
  rejects them anyway with exit 2.
- Negative: A reviewer or future contributor must pattern-match
  the static rule on `shell=True`; a clever bypass (e.g. dynamic
  `shell=True` from a variable) is a hidden risk.

### Alternatives considered
- **Whitelist of allowed commands**: rejected — too restrictive for
  a general-purpose task queue; the user submits arbitrary shell
  commands by design.
- **Allow `shell=True` only for `submit "echo ..."`**: rejected —
  splitting the policy into two paths increases the audit surface.

---

## ADR-011: Secret redaction in `taskq.executor` before `store.update`

### Status
Accepted — 2026-07-28.

### Context
Subprocess output may contain secrets (API keys, tokens). These
can leak to disk via `tasks.json` stdout_tail/stderr_tail, and
once on disk they are a credential-exposure incident. NFR-04
requires 100% redaction of `sk-*` and `token=...` patterns.

### Decision
After `subprocess.run` returns and before `store.update(task_id,
result)` is called, `taskq.executor` runs regex replacement
`(sk-[A-Za-z0-9_-]{8,}|token=\S+)` on `stdout_tail` and
`stderr_tail`. Any line containing a match is replaced entirely
with `[REDACTED]`. The redaction code lives only in `executor` —
no other module is permitted to redact.

### Consequences
- Positive: One chokepoint for redaction; auditable.
- Positive: Test cases (`test_fr02_redaction_*`) cover the regex
  and the line-replacement semantics.
- Negative: A new secret format (e.g. `Bearer xxx`) requires an
  ADR update to the regex — there's no runtime plugin story.
- Negative: Replaces the *entire line*, which can lose legitimate
  context. The SPEC chose the strict policy on purpose.

### Alternatives considered
- **Substring replacement (only the matched span)**: rejected —
  leaves partly-readable secrets and complicates regex maintenance.
- **Allowlist-based redaction (deny all secrets)**: rejected —
  false positives would block legitimate commands from completing.

---

## ADR-012: Centralized config in `taskq.config` (one `os.environ` read site)

### Status
Accepted — 2026-07-28.

### Context
The SPEC defines 8 `TASKQ_*` environment variables (`TASKQ_HOME`,
`TASKQ_MAX_WORKERS`, `TASKQ_TASK_TIMEOUT`, `TASKQ_RETRY_LIMIT`,
`TASKQ_BACKOFF_BASE`, `TASKQ_BREAKER_THRESHOLD`,
`TASKQ_BREAKER_COOLDOWN`, `TASKQ_CACHE_TTL`). NFR-06 requires that
all env reads happen in one place. Without this rule, env reads
sprinkle across the codebase and surprise test isolation.

### Decision
`taskq.config` is the **only** module that reads `os.environ`.
All other modules import typed constants
(`TASKQ_HOME`, `TASKQ_MAX_WORKERS`, …) from `config`. A `reload()`
function is provided for test isolation (callers re-read env after
monkeypatching). `.env.example` documents all 8 vars with comments.

### Consequences
- Positive: One grep point for env vars; one fixture point for
  tests.
- Positive: A teammate reading `taskq.executor` never sees
  `os.environ` — they look up `config.TASKQ_TASK_TIMEOUT`.
- Negative: `reload()` is a global side-effect; tests must be
  careful to call it after monkeypatching.
- Negative: New env vars require changes to `config`, `.env.example`,
  and the docs — a 3-place contract.

### Alternatives considered
- **`pydantic-settings` for typed config**: rejected — non-stdlib
  dependency.
- **Per-module env reads with a shared `Settings` instance**: rejected
  — leaves the door open for ad-hoc env reads and complicates test
  isolation.

---

## ADR-013: Fault injection gated on `--inject-fault=<scenario>` (never on production path)

### Status
Accepted — 2026-07-28.

### Context
NFR-07 requires the system to detect, recover, or fail-fast on
fault-injection scenarios (corrupt-mid-write, oserror-on-write,
disk-full, kill-mid-write). The fault harness must not be reachable
on a normal `python -m taskq ...` invocation — only on a deliberate
test invocation.

### Decision
`taskq.__main__` intercepts `--inject-fault=<scenario>` **before**
`argparse` sees it. The flag is unknown to argparse on the production
path. When present, `__main__` routes to a scenario dispatcher that
sets up the fault and then invokes the normal `cli.main` flow. The
production path (`python -m taskq submit "..."`) never reads an
`--inject-fault` flag.

### Consequences
- Positive: A real user can never accidentally trigger a fault
  scenario by typing `--inject-fault`.
- Positive: Tests can simulate failure modes without monkeypatching
  `store` internals.
- Negative: The flag-handling code in `__main__` is bounded but
  must be carefully tested so that argparse does not see the flag.
- Negative: A new scenario requires a new code path in the
  dispatcher; the set of scenarios is closed at code time.

### Alternatives considered
- **Environment variable `TASKQ_INJECT_FAULT`**: rejected — env
  vars can leak across invocations; a CLI flag is explicit.
- **Monkeypatch `store` in tests**: rejected — does not exercise
  the actual fault handling path; behavior under fault is what
  NFR-07 cares about.

---

## ADR-014: Cache signature = `sha256(command)`; only `done` results are replayable

### Status
Accepted — 2026-07-28.

### Context
`taskq` supports a `--cached` flag that skips subprocess execution
if the same command was previously executed successfully within
`TASKQ_CACHE_TTL` seconds. The cache must avoid replaying
`failed`/`timeout` results; otherwise a transient failure becomes
permanent.

### Decision
- `taskq.cache.signature(command)` returns `sha256(command).hexdigest()`.
- `cache.get(sig)` returns the cached entry only if within TTL and
  the original result state was `done`.
- `cache.put(sig, result)` is called **only** when the result state
  is `done` (per FR-04).
- Cache I/O is atomic-write via `taskq.store` (ADR-003).

### Consequences
- Positive: TTL eviction handles unbounded growth.
- Positive: Strict `done`-only policy prevents transient failures
  from being cached.
- Negative: Two identical commands with different env or filesystem
  state would collide — but the SPEC says the command string is the
  identity, so this is by design.
- Negative: Cache key collisions across distinct commands with the
  same string but different contexts are not distinguished.

### Alternatives considered
- **Hash of `(command, env, cwd)`**: rejected — the SPEC pins
  identity to the command string; expanding the key changes the
  contract.
- **LRU eviction by size**: rejected — TTL is sufficient for the
  documented use case; LRU adds unbounded state.

---

## ADR-015: `argparse` exit-code policy (0/1/2/3/4)

### Status
Accepted — 2026-07-28.

### Context
Downstream tooling (CI, shell scripts) parses the exit code of
`python -m taskq ...`. An inconsistent exit-code policy makes the
CLI brittle.

### Decision
The exit-code table is fixed in `SPEC.md` §7 and enforced in
`taskq.cli`:

| Exit | Meaning |
|------|---------|
| 0 | Success |
| 1 | Internal error (uncaught exception, migration failure) |
| 2 | Validation error or unknown id |
| 3 | Circuit breaker is OPEN |
| 4 | Per-task timeout |

### Consequences
- Positive: Predictable policy; CI can branch on values.
- Positive: A single `cli.main` return value → OS exit code.
- Negative: Adding a new failure mode requires updating the SPEC
  and the table; the mapping is closed at code time.
- Negative: A return value of 1 swallows stack traces unless the
  caller pipes stderr.

### Alternatives considered
- **Use Python's standard exit codes only (0/1/2)**: rejected —
  collapses semantically distinct failure modes (breaker open vs.
  validation error) into one channel.
- **Per-FR exit codes**: rejected — sprawl; the SPEC table is
  small enough to remember.

---

## ADR-016: Fault isolation — `executor` is the only redaction site; `breaker` is the only flapping-state site

### Status
Accepted — 2026-07-28.

### Context
Cross-cutting concerns (redaction, retries, circuit-breaking, fault
injection) tend to spread. Without a one-site rule, refactors add
"defense in depth" paths that look different from one another and
fail unit tests differently.

### Decision
- **Secret redaction** lives only in `taskq.executor` (ADR-011).
- **Circuit breaker state** is owned only by `taskq.breaker`
  (ADR-008). Other modules call `breaker.allow()` / `record()`.
- **Fault injection** is dispatched only from `taskq.__main__`
  (ADR-013).
- **Atomic write / locking** is owned only by `taskq.store`
  (ADR-003 / ADR-004 / ADR-005).
- **Env reads** live only in `taskq.config` (ADR-012).

### Consequences
- Positive: Reviewers audit one site per concern.
- Positive: A new contributor can find the canonical code path
  immediately.
- Negative: A new cross-cutting concern (e.g. telemetry) requires
  an explicit ADR that names its owner module.
- Negative: A "secondary" check inside another module would be a
  smell; reviewers must flag it.

### Alternatives considered
- **Mixins / decorators for cross-cutting concerns**: rejected —
  Python decorator-based AOP obscures the call graph; the SAB
  allowed-dependencies list can't represent it.
- **Aspect-oriented audit at CI time**: rejected — late, brittle,
  and the SAB already enforces architectural invariants.

---

## ADR → Requirement Traceability Matrix

This traceability matrix is the binding bridge between every
architectural decision above, the **requirement** baseline recorded in
`01-requirements/SRS.md`, and the **specification** referenced in
`SPEC.md` v4.0.0. Each row records which SRS requirement and
SPEC.md specification the decision satisfies, and which `taskq`
module owns the runtime implementation that realises the decision.
Gate 1 enforces each FR's acceptance criteria sourced from
`01-requirements/SRS.md` §3, and Gate 2 verifies architecture
compliance against this matrix as ground truth; if a new decision
is added, a row is appended and the SRS section or SPEC.md
specification cited must already exist (or be amended in the same
change). The acceptance criteria described under each FR in
`SRS.md` §3 are the per-row verification target Gate 1 measures this
requirement against.

**Sources cited by every row below**:

- `01-requirements/SRS.md` — Software Requirements Specification
  (project-local srs; mirrors `SPEC.md`).
- `SPEC.md` v4.0.0 — Functional specification (canonical reference).
- `02-architecture/SAD.md` — Software Architecture Document.

| ADR | Decision (FR Served) | NFR Served | SRS requirement reference | SPEC.md specification |
|-----|---------------------|-----------:|---------------------------|-----------------------|
| ADR-001 | Python 3.11 standard library only | NFR-01, NFR-09 | SRS §2 (Constraints) | SPEC §1, §2 |
| ADR-002 | Single-process local CLI with layered modules | NFR-05 | SRS §2, §6; SRS §4 NFR-05 | SPEC §6 |
| ADR-003 | Single persistent-store boundary (`taskq.store`) | NFR-03, NFR-08, NFR-10 | SRS §3 FR-01, FR-02 | SPEC §5.2, §6 |
| ADR-004 | Atomic write via `tmp + os.replace` | NFR-03 | SRS §4 NFR-03 | SPEC §5.2 |
| ADR-005 | Cross-process safety via `fcntl.flock` / `msvcrt.locking` | NFR-08 | SRS §4 NFR-08 | SPEC §5.2 |
| ADR-006 | Schema versioning with `<file>.v<n>.bak` migration backups | NFR-10 | SRS §4 NFR-10 | SPEC §5.2 |
| ADR-007 | ThreadPoolExecutor for `run --all` with shared `store.Lock` | NFR-09 | SRS §3 FR-02; SRS §4 NFR-09 | SPEC §3, §6 |
| ADR-008 | Circuit breaker (CLOSED / OPEN / HALF_OPEN) with `breaker.json` | NFR-03 | SRS §3 FR-03; SRS §4 NFR-03 | SPEC §3 |
| ADR-009 | Injectable `sleep` for deterministic retry tests | — (testability) | SRS §3 FR-03 | SPEC §3 |
| ADR-010 | No `shell=True` — `shlex.split` only | NFR-02 | SRS §3 FR-01, FR-02; SRS §4 NFR-02 | SPEC §3 |
| ADR-011 | Secret redaction in `taskq.executor` before `store.update` | NFR-04 | SRS §4 NFR-04 | SPEC §3 |
| ADR-012 | Centralized config in `taskq.config` | NFR-06 | SRS §4 NFR-06 | SPEC §5.1 |
| ADR-013 | Fault injection gated on `--inject-fault=<scenario>` | NFR-07 | SRS §4 NFR-07 | SPEC §5.3 |
| ADR-014 | Cache signature = `sha256(command)`; only `done` replays | — (cross-cutting) | SRS §3 FR-04 | SPEC §3 |
| ADR-015 | Argparse exit-code policy (0/1/2/3/4) | — (CLI contract) | SRS §3 FR-05 | SPEC §7 |
| ADR-016 | Fault isolation — one site per cross-cutting concern | NFR-02, NFR-04, NFR-07 | SRS §4 NFR-02, NFR-04, NFR-07 | SPEC §3, §5 |

**Coverage statement**: every NFR-01 through NFR-10 declared by the
project-local `01-requirements/SRS.md` and the corresponding
`SPEC.md` §4 specification is bound to at least one decision above.
Cross-cutting NFRs whose satisfaction is shared across modules
(performance NFR-01 / NFR-09, security NFR-02, reliability NFR-03,
evolvability NFR-10, concurrency NFR-08) have a single canonical
owning ADR; test-side and operational enforcement (gitleaks, fault
injection, atomic-write verification) is out of scope of the
traceability matrix and is enforced separately at Gate 2–4. If an
NFR is added to the SRS or specification in a future change, the
corresponding ADR row is updated in lockstep so the traceability
matrix never loses a binding to its requirement or specification.

---

*Document version: ADR v1.0.0 for taskq SPEC.md v4.0.0 / SAD.md v1.0.0 — 2026-07-28*
