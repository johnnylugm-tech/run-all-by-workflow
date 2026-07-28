# Software Architecture Document (SAD) — taskq

> Authoritative architecture for the `taskq` local task-queue CLI. Canonical
> structure follows `SPEC.md` v4.0.0 §6 — 9 modules under `src/taskq/`.
> All module names referenced in §5 (SAB) and §6 (SEC) are quoted exactly
> as they appear in `SPEC.md` §6 so the SAB / SEC parsers can resolve them.

---

## 1. Architecture Overview

`taskq` is a single-process local task-queue CLI written in Python 3.11 with
**zero runtime external dependencies** (standard library only). The runtime
form is `python -m taskq <subcommand>`. The architecture is intentionally
flat and layered: a thin CLI dispatch layer (`cli`, `__main__`) sits on top
of three execution-lifecycle modules (`executor`, `breaker`, `cache`) which
all read/write through a single persistent-store abstraction (`store`,
`models`) and a single configuration loader (`config`).

| Layer | Modules | Responsibility |
|-------|---------|----------------|
| Entry / CLI | `taskq.__main__`, `taskq.cli` | argparse wiring + exit-code policy (FR-05) |
| Execution | `taskq.executor`, `taskq.breaker`, `taskq.cache` | subprocess run + retry/backoff, circuit breaker, TTL cache (FR-02/03/04) |
| Persistence | `taskq.store`, `taskq.models` | atomic JSON storage of tasks/breaker/cache + data classes (FR-01/02; NFR-03/08/10) |
| Configuration | `taskq.config` | 8 `TASKQ_*` env vars (NFR-06) |

**Architecture invariants** (matches `SPEC.md` §10 framework alignment):

1. **Single direction of dependency.** `cli` → `{executor, breaker, cache}` →
   `store` → `models`; `store` → `config`; no upward calls. This honors the
   `harness/CLAUDE.md` constraint `no_circular_dependencies`.
2. **One persistent boundary.** All three JSON files (`tasks.json`,
   `breaker.json`, `cache.json`) flow through `taskq.store.atomic_write`,
   which is the *only* code path that owns the atomic-replace pattern
   (`tmp + os.replace`) and the file lock acquisition. Cross-process safety
   (NFR-08) is enforced here, not at call sites.
3. **Cross-cutting concerns are explicit, not implicit.** Configuration is
   read once in `config.py` (NFR-06); secret redaction (NFR-04) lives only
   in `executor` between subprocess return and `store` write; fault
   injection (NFR-07) is gated on a CLI flag handled in `__main__` and
   never active on the production path.

### 1.1 System Verification Target

> **Phase 3 Gate 2 Requirement**: harness runs `make verify-system`. Add a
> `verify-system` target to `Makefile` that runs integration + smoke tests.

**Makefile target**: `verify-system`

---

## 2. Module Design

The directory layout is fixed by `SPEC.md` §6 (9 modules under
`src/taskq/`). Total files-per-directory = 9 (≤ 15 cap, no god-module).
Per FR–module traceability is recorded below and also in §5 (`fr_module_traceability`).

### 2.1 FR → Module mapping

| FR | Primary module(s) | Supporting module(s) | Rationale |
|----|-------------------|----------------------|-----------|
| **FR-01** Submit + validation | `taskq.cli` (orchestration), `taskq.executor` (none — pure validation) | `taskq.models` (Task dataclass + state constants `PENDING`); `taskq.store` (atomic persist); `taskq.config` (env-driven paths) | Validation rules live in `cli.submit` because they are dispatch-layer concerns; persistence is delegated to `store` because the atomic-write boundary already exists |
| **FR-02** Task executor | `taskq.executor` (`run`, `run_all`) | `taskq.models` (state transitions `pending→running→done|failed|timeout`); `taskq.store` (`update` under shared `Lock`); `taskq.config` (`TASKQ_TASK_TIMEOUT`, `TASKQ_MAX_WORKERS`) | Subprocess + retry/dispatch live together so all subprocess-adjacent concerns (timeout, redaction, sleep injection) are co-located |
| **FR-03** Retry + circuit breaker | `taskq.executor` (retry loop + exponential backoff with injectable sleep); `taskq.breaker` (CLOSED/OPEN/HALF_OPEN state machine) | `taskq.store` (atomic read/write of `breaker.json`); `taskq.config` (`TASKQ_RETRY_LIMIT`, `TASKQ_BACKOFF_BASE`, `TASKQ_BREAKER_THRESHOLD`, `TASKQ_BREAKER_COOLDOWN`) | Retry is per-task; breaker is cross-task + cross-process. Two different scopes → two modules |
| **FR-04** TTL cache | `taskq.cache` (`signature`, `get`, `put`) | `taskq.executor` (calls `cache.get` before run, `cache.put` after done); `taskq.store` (atomic read/write of `cache.json`); `taskq.config` (`TASKQ_CACHE_TTL`) | Cache reads sit *before* subprocess; writes sit *after* — both inside `executor`. Pure cache I/O lives in `cache` |
| **FR-05** CLI integration | `taskq.cli` (argparse subcommands), `taskq.__main__` (entry) | All other modules (dispatch hub) | Per `SPEC.md` §10, `taskq.cli` is the canonical dispatch + exit-code policy owner |

### 2.2 Module responsibilities

#### 2.2.1 `taskq.__main__`
| Attribute | Value |
|-----------|-------|
| Responsibility | `python -m taskq` entry; parse `sys.argv` (or unit-test argv); forward to `taskq.cli.main`. Owns `--inject-fault` flag routing for NFR-07 (test-only, never on production path). |
| External Interface | `__main__.py: def main() -> int` (returns exit code). |
| Dependencies | `taskq.cli`, `taskq.config`. |
| FR mapped | FR-05 |

**Logical constraints**
- The `--inject-fault` flag MUST be unknown to argparse on the production
  path; it is intercepted before `cli.main` and routes to the fault
  scenario dispatcher. This guarantees the flag never silently enables
  fault scenarios when a real user types it (`SPEC.md` §5.3).
- `__main__.py` MUST NOT import `taskq.executor` or `taskq.store`
  directly; all access goes through `cli.main`.

#### 2.2.2 `taskq.cli`
| Attribute | Value |
|-----------|-------|
| Responsibility | argparse subcommand wiring (`submit`, `run`, `status`, `list`, `clear`); per-subcommand argument validation; global `--json`; canonical exit-code policy (0/1/2/3/4). |
| External Interface | `taskq.cli.main(argv: list[str] \| None = None) -> int`. |
| Dependencies | `taskq.executor`, `taskq.breaker`, `taskq.cache`, `taskq.store`, `taskq.config`, `taskq.models` (validation helpers). |
| FR mapped | FR-01, FR-03, FR-04, FR-05 |

**Logical constraints**
- Validation rules for FR-01 (empty, length, injection blacklist, name
  uniqueness) live here because `submit` is a dispatch-layer command and
  must reject before any storage I/O (NFR-02).
- All stdout emitted under `--json` MUST be a single JSON object on one
  line (no pretty-print) so downstream tooling can pipe-parse.
- Exit-code table per `SPEC.md` §7 is the single source of truth: 0
  success / 2 validation + unknown id / 3 breaker open / 4 single-task
  timeout / 1 internal error.

#### 2.2.3 `taskq.executor`
| Attribute | Value |
|-----------|-------|
| Responsibility | Subprocess execution (`subprocess.run(shlex.split(command), capture_output=True, text=True, timeout=...)`); result-trimming (`stdout_tail`/`stderr_tail` last 2000 chars); timeout mapping; retry loop with exponential backoff + injectable sleep; **secret redaction** (NFR-04) before persistence; `--cached` short-circuit. |
| External Interface | `taskq.executor.run(task_id: str, use_cache: bool = False) -> int`; `taskq.executor.run_all() -> int`. |
| Dependencies | `taskq.store`, `taskq.cache`, `taskq.breaker`, `taskq.config`. |
| FR mapped | FR-02, FR-03, FR-04 |

**Logical constraints**
- **No** call site may use `shell=True` (NFR-02). Enforced by:
  (a) a static check rule (`shell=True` absence is a Gate 1 verification),
  (b) `executor.run` taking the raw command string and calling
  `shlex.split` internally — call sites never pass args arrays.
- The `sleep` function used between retries MUST be injectable
  (`_sleep: Callable[[float], None] = time.sleep` default) so retry
  timing can be unit-tested deterministically (SPEC §3 FR-03).
- Redaction MUST run on `stdout_tail`/`stderr_tail` *before* they reach
  `store.update`; the regex `(sk-[A-Za-z0-9_-]{8,}|token=\S+)` replaces
  the entire line containing a match with `[REDACTED]` (NFR-04).
- `executor.run` calls `breaker.allow()` first; if the breaker is OPEN,
  `run` writes no subprocess and returns exit-3 sentinel; `cli` translates
  that to OS exit code 3 (FR-03).

#### 2.2.4 `taskq.breaker`
| Attribute | Value |
|-----------|-------|
| Responsibility | Global circuit breaker state machine across tasks and processes (CLOSED → OPEN → HALF_OPEN); atomic persistence of `breaker.json`; cross-process visibility. |
| External Interface | `taskq.breaker.allow() -> bool`; `taskq.breaker.record(success: bool) -> None`; `taskq.breaker.state() -> Literal["CLOSED","OPEN","HALF_OPEN"]`. |
| Dependencies | `taskq.store` (atomic read/write, flock acquire); `taskq.config` (`TASKQ_BREAKER_THRESHOLD`, `TASKQ_BREAKER_COOLDOWN`). |
| FR mapped | FR-03 |

**Logical constraints**
- State transitions are read-modify-write under the `store` lock so two
  concurrent processes cannot both transition from HALF_OPEN and pass two
  trial tasks (NFR-08 cross-process).
- `breaker.json` follows `SPEC.md` §5.2 schema
  `{version:1, state, failure_count, opened_at}` and MUST be migrated to
  v1 on read if older (NFR-10) — `breaker` delegates schema handling to
  `store`.
- All `print` / `logging` from `breaker` that reaches users goes through
  `cli`, not directly to stderr.

#### 2.2.5 `taskq.cache`
| Attribute | Value |
|-----------|-------|
| Responsibility | TTL cache: signature = `sha256(command)`; replay of `done` results within `TASKQ_CACHE_TTL` seconds; atomic read/write of `cache.json`; expiry eviction. |
| External Interface | `taskq.cache.signature(command: str) -> str`; `taskq.cache.get(sig: str) -> dict \| None`; `taskq.cache.put(sig: str, result: dict) -> None`. |
| Dependencies | `taskq.store`, `taskq.config` (`TASKQ_CACHE_TTL`). |
| FR mapped | FR-04 |

**Logical constraints**
- `cache.get` MUST be called by `executor.run` *before* `subprocess.run`
  when `use_cache` is true; on hit, no subprocess is spawned (SPEC §3
  FR-04).
- Cache contents persist only successful `done` results; `failed`,
  `timeout`, or partially-set entries are not written (only `done` is
  replayable per SPEC §3 FR-04).
- Cache I/O reuses `taskq.store` for atomic-write + flock semantics, so
  concurrent `cache.get` / `cache.put` is safe alongside executor writes
  (NFR-03, NFR-08).

#### 2.2.6 `taskq.store`
| Attribute | Value |
|-----------|-------|
| Responsibility | Single I/O boundary for all three JSON files; atomic write (`tmp + os.replace`); in-process `threading.Lock`; cross-process file lock (`fcntl.flock` on POSIX, `msvcrt.locking` on Windows); schema-version migration (NFR-10); fault-injection hooks (NFR-07). |
| External Interface | `taskq.store.atomic_write(path, payload) -> None`; `taskq.store.read(path) -> dict`; `taskq.store.under_lock(fn) -> T` (context manager for shared Lock). |
| Dependencies | `taskq.config` (`TASKQ_HOME`); `taskq.models` (defaults for missing fields). |
| FR mapped | FR-01, FR-02 (high-risk: `SPEC.md` §10) |

**Logical constraints**
- The shared `Lock` is process-local; cross-process safety comes from
  `flock` (POSIX) or `msvcrt.locking` (Windows). NFS / network FS
  detection causes a graceful degradation: no flock, but atomic write is
  preserved and a `WARNING` is logged (NFR-08).
- Schema migration (NFR-10) happens lazily on `read`: missing or older
  `version` is bumped to 1 and the file is rewritten *with a backup*
  `<file>.v<n>.bak` left in place; migration failures exit 1 (fail-fast).
- All fault-injection simulation points (corrupt-mid-write, oserror-on-write,
  disk-full, kill-mid-write) are reached **only** through the
  `--inject-fault=<scenario>` dispatcher in `__main__`; production code
  paths cannot trigger them (SPEC §5.3, NFR-07).

#### 2.2.7 `taskq.models`
| Attribute | Value |
|-----------|-------|
| Responsibility | Data classes for tasks (id, command, name, status, result fields, created_at, finished_at); status enum/constants; cache-entry schema; breaker-state dataclass. No I/O — pure data definition. |
| External Interface | `taskq.models.Task`, `taskq.models.TaskStatus`, `taskq.models.BreakerState`, `taskq.models.CacheEntry`. |
| Dependencies | none (stdlib `dataclasses`, `enum`). |
| FR mapped | FR-01, FR-02, FR-04 |

**Logical constraints**
- All public classes/fields carry docstrings with `[FR-XX]` / `[NFR-XX]`
  citations (NFR-05 — `test_coverage_docstring_fr_refs`).
- `models.py` MUST NOT import from `cli`, `executor`, `breaker`, `cache`,
  `store`, `config` — it is the lowest layer.

#### 2.2.8 `taskq.config`
| Attribute | Value |
|-----------|-------|
| Responsibility | One place that reads the 8 `TASKQ_*` env vars with documented defaults; exposes typed constants (paths, ints, floats); never reads env vars elsewhere in the codebase (NFR-06). |
| External Interface | `taskq.config.TASKQ_HOME`, `taskq.config.TASKQ_MAX_WORKERS`, ..., `taskq.config.TASKQ_CACHE_TTL`; `taskq.config.reload() -> None` for tests. |
| Dependencies | `os.environ`, `pathlib`. |
| FR mapped | FR-05 (configuration dispatch); NFR-06 |

**Logical constraints**
- `config.py` is read by all other modules but reads env vars in exactly
  one location — at import time, with `reload()` available for test
  isolation.
- All 8 vars appear in `.env.example` with comments per NFR-06.

---

## 3. Interfaces & Data Flows

### 3.1 Subcommand interfaces (FR-05)

| Subcommand | argv shape | Returns exit | Maps to FR |
|------------|-----------|--------------|------------|
| `submit "<cmd>" [--name N]` | `cli.submit` → validate → `store.put` → print id | 0 / 2 | FR-01 |
| `run <id> [--cached]` | `cli.run_one` → `executor.run` → optional `breaker.allow` → `cache.get`/`cache.put` | 0 / 2 / 3 / 4 | FR-02 / FR-03 / FR-04 |
| `run --all` | `cli.run_all` → `executor.run_all` → `ThreadPoolExecutor(max_workers=TASKQ_MAX_WORKERS)` with shared `store.Lock` | 0 / 3 | FR-02 / FR-03 |
| `status <id>` | `cli.status` → `store.get` | 0 / 2 | FR-05 |
| `list [--status S]` | `cli.list_tasks` → streaming iterator over `store.iter_tasks` (NFR-09) | 0 / 2 | FR-05 |
| `clear` | `cli.clear` → delete all 3 JSON files under `$TASKQ_HOME` | 0 | FR-05 |
| `--json` (global) | Pretty-print OFF, single-line JSON, no progress noise | — | FR-05 |

### 3.2 Data flow: `submit` → `run --all` → cache replay

```
                ┌─────────────────────────────────────┐
   user ───►    │  python -m taskq ( __main__ )       │
                │       │                             │
                │       ▼                             │
                │  taskq.cli ( argparse, exit codes ) │
                └──┬─────────────┬──────────┬─────────┘
                   │ validate    │ dispatch │ JSON encode
        FR-01     ▼             ▼          ▼
                cli.submit    cli.run_one  ( --json )
                   │             │
                   ▼             ▼
              taskq.store   taskq.executor
              (atomic        ├──► taskq.breaker (OPEN? allow?)
               write)       ├──► taskq.cache  ( --cached ? get/put )
                            └──► subprocess.run ( shlex.split, no shell=True )
                                   │
                                   ▼
                             result { exit_code, stdout_tail[last 2000],
                                       stderr_tail[last 2000],
                                       duration_ms, finished_at }
                                   │
                                   ▼
                          executor: REDACT secrets  ( NFR-04 )
                                   │
                                   ▼
                          taskq.store.update ( atomic, under Lock, flock )
```

### 3.3 Storage layout (`$TASKQ_HOME/`)

```
$TASKQ_HOME/
├── tasks.json      # {version:1, tasks:{id -> Task}}
├── breaker.json    # {version:1, state, failure_count, opened_at}
├── cache.json      # {version:1, entries:{sig -> {result, cached_at}}}
└── *.v<n>.bak      # schema-migration backups (NFR-10)
```

All three JSON files are written via `taskq.store.atomic_write` so the
NVF fault-injection scenarios (NFR-07) and the cross-process safety
(NFR-08) guarantee is centralized.

### 3.4 Module dependency edges (no cycles)

```
__main__ ──► cli
cli       ──► executor ──► { store, cache, breaker, config }
cli       ──► store, breaker, cache, models, config
executor  ──► { store, cache, breaker, config }
breaker   ──► { store, config }
cache     ──► { store, config }
store     ──► { config, models }
models    ──► (stdlib only)
config    ──► (stdlib only)
```

Verifies the framework constraint `no_circular_dependencies` from
`SPEC.md` §10 and `harness/CLAUDE.md`.

---

## 4. NFR Handling

Each NFR enumerated from `SPEC.md` §4 (and reflected in `SRS.md` §4) is
mapped to the module(s) responsible and the verification method. The SAB
block (§5) carries machine-readable copies of the enforceable subset.

| NFR | Category | Target | Owner module(s) | Verification |
|-----|----------|--------|-----------------|--------------|
| **NFR-01** | performance | `submit` + `status` 100-iter p95 `< 50ms` | `taskq.cli`, `taskq.store` | pytest-benchmark (Gate 1) |
| **NFR-02** | security | `shell=True` count == 0; injection blacklist tests | `taskq.cli`, `taskq.executor` | static scan + `test_fr01_injection_blacklist_*` |
| **NFR-03** | reliability | atomic JSON for all 3 files; breaker recovery ≤ `TASKQ_BREAKER_COOLDOWN` + 1s | `taskq.store`, `taskq.breaker` | `test_atomic_write_*` + integration |
| **NFR-04** | security | secrets redaction (sk-*, token=) — 100% hit rate | `taskq.executor` (between subprocess return and `store.update`) | `test_fr02_redaction_*` |
| **NFR-05** | maintainability | 100% docstring `[FR-XX]` citation on public API | `taskq.models`, all modules | `test_docstring_fr_refs` |
| **NFR-06** | deployability | 8 `TASKQ_*` env vars centralised in `config.py` + `.env.example` | `taskq.config` | static scan + `.env.example` lint |
| **NFR-07** | resilience | fault injection detected/recovered/fail-fast (never silent) | `taskq.store`, `taskq.__main__` | `--inject-fault` tests + monkeypatch |
| **NFR-08** | concurrency | cross-process flock; network-FS graceful downgrade | `taskq.store` | 4-process subprocess test |
| **NFR-09** | scalability | 1000-task p95 `< 100ms`; run-all 100 tasks no JSON loss; memory `< 100MB` | `taskq.cli`, `taskq.store` | pytest-benchmark scaled + memory profile |
| **NFR-10** | evolvability | schema version field + migration v<n>→1 with backup | `taskq.store` | `test_schema_migration_*` |

The full machine-readable mapping is mirrored in §5 under
`nfr_traceability`.

---

## 5. SAB Block (machine-readable — BINDING CONTRACT)

> **CONTRACT**: Field names, types, `sab:` root key, and `phase` as int must
> match `core/quality_gate/sab_parser.py:render_canonical_sab_template()`.
> The YAML below is the canonical template with EXAMPLE values replaced by
> real project values for `taskq`. Validation:
> `python3 scripts/generate_sab.py --validate --project .`

<!-- SAB:START -->
```yaml
sab:
  version: "1.0"
  created_at: "2026-07-28"
  phase: 2  # MUST be int, NOT a string — parser raises on 'phase: "2"'
  project: "taskq"

  layers:
    - name: entry
      modules:
        - name: "taskq.__main__"
        - name: "taskq.cli"
      allowed_dependencies: ["execution", "persistence", "config"]

    - name: execution
      modules:
        - name: "taskq.executor"
        - name: "taskq.breaker"
        - name: "taskq.cache"
      allowed_dependencies: ["persistence", "config"]

    - name: persistence
      modules:
        - name: "taskq.store"
        - name: "taskq.models"
      allowed_dependencies: ["config"]

    - name: config
      modules:
        - name: "taskq.config"
      allowed_dependencies: []

  allowed_dependencies:
    - from: entry
      to: execution
    - from: entry
      to: persistence
    - from: entry
      to: config
    - from: execution
      to: persistence
    - from: execution
      to: config
    - from: persistence
      to: config

  quality_targets:
    max_complexity: 15
    min_coverage: 80
    max_coupling: 0.3

  nfr_dimension_mapping: {}  # OPTIONAL — auto-derived from nfr_traceability.type

  nfr_traceability:
    NFR-01:
      type: performance
      target: "p95 < 50ms"
      module: taskq.store
    NFR-02:
      type: security
      target: "shell=True count == 0"
      module: taskq.executor
    NFR-03:
      type: reliability
      target: "100% atomic write; recovery <= cooldown+1s"
      module: taskq.store
    NFR-04:
      type: security
      target: "100% redaction hit rate"
      module: taskq.executor
    NFR-05:
      type: maintainability
      target: "100% docstring [FR-XX] coverage"
      module: taskq.models
    NFR-06:
      type: deployability
      target: "8 TASKQ_* env vars centralised"
      module: taskq.config
    NFR-07:
      type: reliability
      target: "no silent loss on fault injection"
      module: taskq.store
    NFR-08:
      type: reliability
      target: "100% cross-process flock; no JSON corruption"
      module: taskq.store
    NFR-09:
      type: performance
      target: "p95 < 100ms @ 1000 tasks; memory < 100MB"
      module: taskq.store
    NFR-10:
      type: maintainability
      target: "100% schema migration success with backup"
      module: taskq.store

  advisory_only: []  # AUTO-FILLED by parser — omit or leave []

  gate_score_overrides: {}  # AUTO-DERIVED by parser — omit or leave {}

  fr_module_traceability:
    FR-01: "taskq.cli"
    FR-02: "taskq.executor"
    FR-03: "taskq.breaker"
    FR-04: "taskq.cache"
    FR-05: "taskq.cli"

  architecture_constraints:
    - "no_circular_dependencies"

  high_risk_modules:
    - "taskq.executor"
    - "taskq.store"
```
<!-- SAB:END -->

Note: This SAD-side placeholder is the initial binding contract. The
authoritative SAB is regenerated by `python3 scripts/generate_sab.py
--project . [--overwrite]` during the SAB Generation phase (P3).

---

## 6. Security Design (STRIDE-lite — machine-readable, BINDING CONTRACT)

> **CONTRACT**: Field names and the `security_design:` root key are parsed
> by `core/quality_gate/security_design.py:extract_security_block()`. The
> YAML below is the canonical template (verbatim from
> `render_canonical_security_template()`) with EXAMPLE values replaced by
> the real `taskq` project values. `taskq` has a real attack surface
> (subprocess command injection, secret leakage in process output,
> cross-process race), so `applicability: full` is the honest declaration.
> Validation: `python3 harness_cli.py check-artifact-consistency --project .`

Three trust boundaries are identified:

- **TB-01 User → CLI** — unauthenticated user commands entering via
  `python -m taskq` (FR-01/05).
- **TB-02 CLI → Local subprocess** — shell commands spawned by
  `taskq.executor` (FR-02/03/04; NFR-02).
- **TB-03 Subprocess → on-disk store** — task/breaker/cache JSON files
  shared across processes and processes-themselves reading their output
  (NFR-03/04/08/10).

<!-- SEC:START -->
```yaml
security_design:
  version: "1.0"
  applicability: full   # full | none — none REQUIRES justification and skips the rest
  justification: ""     # required (>=20 chars) when applicability: none
  trust_boundaries:
    - id: TB-01
      name: "user to CLI input"
      description: "unauthenticated argv reaching taskq.cli via python -m taskq (FR-05)"
    - id: TB-02
      name: "CLI to local subprocess"
      description: "validated command strings handed to taskq.executor and shlex.split subprocess.run (FR-02; NFR-02)"
    - id: TB-03
      name: "subprocess output to on-disk store"
      description: "stdout_tail/stderr_tail of subprocess persisted into shared $TASKQ_HOME JSON files across processes (NFR-03/04/08/10)"
  threats:
    - id: T-01
      boundary: TB-01
      category: tampering
      description: "user submits a command string containing shell-metacharacter injection"
      mitigation: "FR-01 injection blacklist rejects ; | & $ > < ` with exit 2 before any storage write"
      owner_module: "taskq.cli"
      nfr: NFR-02
      verified_by: "test_fr01_injection_blacklist_rejected"
    - id: T-02
      boundary: TB-02
      category: elevation_of_privilege
      description: "executor falls back to shell=True during a refactor"
      mitigation: "static rule forbids shell=True anywhere in src/taskq; enforced at Gate 1 + tests exercise shlex.split"
      owner_module: "taskq.executor"
      nfr: NFR-02
      verified_by: "test_no_shell_true_anywhere"
    - id: T-03
      boundary: TB-02
      category: denial_of_service
      description: "subprocess hangs forever and consumes worker slots / threads"
      mitigation: "FR-02 timeout via subprocess.run(timeout=TASKQ_TASK_TIMEOUT); non-zero state timeout"
      owner_module: "taskq.executor"
      nfr: NFR-02
      verified_by: "test_fr02_timeout_cancels_subprocess"
    - id: T-04
      boundary: TB-03
      category: information_disclosure
      description: "stdout_tail/stderr_tail accidentally persisted to disk carrying secrets like sk-* API keys or token=... values"
      mitigation: "NFR-04 redacts lines matching (sk-[A-Za-z0-9_-]{8,}|token=\\S+) to [REDACTED] before store.update"
      owner_module: "taskq.executor"
      nfr: NFR-04
      verified_by: "test_fr02_redaction_replaces_secret_lines"
    - id: T-05
      boundary: TB-03
      category: tampering
      description: "two concurrent python -m taskq processes corrupt tasks.json on interleaved write"
      mitigation: "NFR-08 fcntl.flock + atomic os.replace under taskq.store; network FS degrades with WARNING but keeps atomic write"
      owner_module: "taskq.store"
      nfr: NFR-08
      verified_by: "test_fr_cross_process_flock_no_corruption"
    - id: T-06
      boundary: TB-03
      category: repudiation
      description: "schema migration overwrites a v0 tasks.json without leaving a recoverable backup"
      mitigation: "NFR-10 backs the original file up to <file>.v<n>.bak before migration; failure exits 1 fail-fast"
      owner_module: "taskq.store"
      nfr: NFR-10
      verified_by: "test_schema_migration_v0_backup_present"
    - id: T-07
      boundary: TB-01
      category: spoofing
      description: "user supplies a --name duplicating an existing pending/running task to silently shadow state"
      mitigation: "FR-01 name-uniqueness rule rejects duplicate --name with exit 2"
      owner_module: "taskq.cli"
      nfr: NFR-02
      verified_by: "test_fr01_duplicate_name_rejected"
```
<!-- SEC:END -->

Note: `owner_module` names a module declared in §5; `nfr` references
`SPEC.md` §4 IDs that exist in `SRS.md` §4; `verified_by` is a single
test name (multi-test threats would be split into separate T-NN entries,
which is why T-03 and T-05 cover the timeout/lock concerns independently
rather than combining). `test_*` names refer to `tests/` functions that
Phase 5+ must materialise; `bug-hunt-targets` will use these threat IDs
to drive adversarial review, and `derive_test_cases.md` Step 1c forces
the NFR-pattern test cases regardless of SRS keyword presence.

---

*Document version: SAD v1.0.0 for taskq SPEC.md v4.0.0 — 2026-07-28*
