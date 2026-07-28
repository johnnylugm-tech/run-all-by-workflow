# Software Requirements Specification (SRS) — taskq

## 1. Introduction

`taskq` is a local task-queue command-line tool. It submits shell commands as tasks, executes them with controlled timeout, retry, circuit-breaker, concurrency, and TTL-cache behavior, and provides status, listing, and storage-clearing commands. The implementation target is Python 3.11 with zero runtime external dependencies, entered through `python -m taskq`. Canonical source: `SPEC.md` v4.0.0 (2026-07-11), §§1–11.

## 2. Constraints

- Runtime uses Python 3.11 standard library only; CLI entry is `python -m taskq`. (SPEC §§1–2)
- Execution uses `subprocess.run(shlex.split(...))`; `shell=True` is forbidden. Concurrency uses `ThreadPoolExecutor`; shared storage uses a thread lock. (SPEC §2)
- `tasks.json`, `breaker.json`, and `cache.json` use temporary-file plus `os.replace` atomic writes. (SPEC §§2, 5.2)
- Eight `TASKQ_*` environment variables are read by `config.py`, with canonical defaults. (SPEC §5.1)
- The canonical module layout is `src/taskq/{__init__,__main__,config,models,store,executor,breaker,cache,cli}.py`. (SPEC §6)

## 3. Functional Requirements

### FR-01: 任務提交與驗證

`taskq submit "<command>" [--name NAME]` validates the command as follows: empty or all-whitespace commands are rejected; commands longer than 1000 characters are rejected; commands containing any of `; | & $ > < \`` are rejected; and a `--name` duplicate among existing pending/running tasks is rejected. Any violation emits an error on stderr, writes no storage, and exits 2. (SPEC §3, FR-01)

On success, the tool creates a task id from the first 8 hexadecimal characters of `uuid4`, records status `pending`, `command`, `name`, and `created_at`, atomically writes `$TASKQ_HOME/tasks.json`, and prints the task id. With `--json`, stdout is one JSON object containing `id` and `status: "pending"`. (SPEC §3, FR-01)

Acceptance criteria:
- `submit "echo hi"` returns an 8-hex id and pending status.
- Empty, over-length, injection-character, and duplicate-name submissions exit 2, report stderr, and do not write the task.
- Successful task storage is atomically persisted in `tasks.json`.

### FR-02: 任務執行器

`taskq run <id>` and `taskq run --all` execute tasks using `subprocess.run(shlex.split(command), capture_output=True, text=True, timeout=TASKQ_TASK_TIMEOUT)`; no path uses `shell=True`. Task state follows `pending → running → done | failed | timeout`. Exit code 0 produces `done`; non-zero produces `failed`; `TimeoutExpired` produces `timeout`. Results record `exit_code`, the last 2000 characters of `stdout_tail` and `stderr_tail`, `duration_ms`, and `finished_at`. (SPEC §3, FR-02)

`run --all` concurrently executes all pending tasks with `ThreadPoolExecutor(max_workers=TASKQ_MAX_WORKERS)`, and storage writes are thread-safe through a shared lock. In single-task mode, a timeout result exits 4. (SPEC §3, FR-02)

Acceptance criteria:
- A successful command reaches `done` with exit code 0 and recorded result fields.
- Non-zero and timeout commands reach their specified states; single-task timeout exits 4.
- `run --all` executes all pending tasks concurrently without task loss or invalid `tasks.json`.

### FR-03: 重試與斷路器

When a run result is `failed` or `timeout`, the tool retries automatically up to `TASKQ_RETRY_LIMIT` times. Before retry number `n`, it waits `TASKQ_BACKOFF_BASE × 2^n` seconds; the sleep function is injectable for testing. (SPEC §3, FR-03)

The global circuit breaker is shared across tasks and processes. When consecutive final failures (after retries are exhausted) reach `TASKQ_BREAKER_THRESHOLD`, state becomes `OPEN`. While `OPEN`, any run is immediately rejected with exit 3 and stderr `breaker open`, without executing a subprocess. After `TASKQ_BREAKER_COOLDOWN` seconds it enters `HALF_OPEN` and permits one task; success changes state to `CLOSED` and resets the count, while failure returns to `OPEN`. State is atomically persisted in `$TASKQ_HOME/breaker.json`. (SPEC §3, FR-03)

Acceptance criteria:
- Failed/timeout tasks retry at the configured cap and exponential delays.
- Threshold consecutive final failures open the breaker; an open breaker rejects execution with exit 3.
- Cooldown permits one half-open trial with the specified success/failure transitions.

### FR-04: 結果 TTL 快取

The cache signature is `sha256(command)`. For `taskq run <id> --cached`, a recent `done` result with the same signature and age within `TASKQ_CACHE_TTL` is replayed directly, without subprocess execution; the task becomes `done` with `cached: true`, retaining `exit_code` and `stdout_tail`. Missing or expired cache entries execute normally, and successful results are written to `$TASKQ_HOME/cache.json`. Cache reads and writes are atomic and thread-safe alongside FR-02 concurrency. (SPEC §3, FR-04)

Acceptance criteria:
- A valid TTL entry replays without subprocess execution and marks the task cached.
- Missing or expired entries execute and create a cache entry after success.
- Concurrent cache access remains atomic and thread-safe.

### FR-05: CLI 整合

The `python -m taskq` entry uses argparse subcommands: `submit "<cmd>" [--name N]`; `run <id> [--cached]` or `run --all`; `status <id>` to output all task fields; `list [--status S]` to list optionally filtered tasks; and `clear` to clear all data files under `$TASKQ_HOME`. A global `--json` flag emits machine-readable single-line JSON. Exit codes are 0 success, 2 input validation errors including unknown task id, 3 breaker open, 4 single-task timeout, and 1 other internal errors. (SPEC §3, §7)

Acceptance criteria:
- Every listed subcommand performs its specified behavior.
- `--json` output is a single machine-readable JSON line.
- Error scenarios use the canonical exit-code map and messages.

## 4. Non-Functional Requirements

### NFR-01: Performance

The combined `submit` and `status` operation, excluding subprocess execution, must have p95 latency below 50 ms over 100 iterations, measured with pytest-benchmark. (SPEC §4, §11)

Acceptance criterion: the benchmark p95 is `< 50ms`.

### NFR-02: Security

The entire codebase must not use `shell=True`; FR-01 injection blacklist behavior must have test coverage. (SPEC §4, §11)

Acceptance criterion: code scan finds zero `shell=True` uses and injection tests cover all listed characters.

### NFR-03: Reliability

All three data files must use temporary-file plus `os.replace` atomic writes; after process interruption each remains valid JSON. Breaker `OPEN → CLOSED` recovery must be no later than `TASKQ_BREAKER_COOLDOWN + 1s`. (SPEC §4, §11)

Acceptance criterion: interruption and recovery tests verify valid JSON and the recovery bound.

### NFR-04: Secret Redaction

Before persistence, each line matching `(sk-[A-Za-z0-9_-]{8,}|token=\S+)` in `stdout_tail` or `stderr_tail` is replaced in its entirety with `[REDACTED]`. (SPEC §4, §11)

Acceptance criterion: redaction tests achieve 100% matching-line replacement.

### NFR-05: Maintainability

All public functions and classes in `src/taskq` must have docstrings containing an `[FR-XX]` reference. (SPEC §4, §10–11)

Acceptance criterion: inspection reports 100% public callable coverage.

### NFR-06: Deployability

All eight `TASKQ_*` parameters must be read from environment variables by `config.py` with canonical defaults, and `.env.example` must declare and annotate each one. (SPEC §4, §5.1)

Acceptance criterion: `.env.example` contains all eight names and configuration tests verify defaults and overrides.

### NFR-07: Resilience

For `tasks.json`, `breaker.json`, and `cache.json`, fault scenarios of mid-write corruption, simulated `OSError`, simulated disk full, and simulated kill during write must either recover on next startup from backup or fail fast with explicit stderr and an explicit non-zero exit code. Silent rebuild and swallowed errors are forbidden. Fault injection is enabled only through `--inject-fault=<scenario>` or test monkeypatch; the normal execution path does not enable it. (SPEC §4, §5.3)

Acceptance criterion: each scenario achieves recovery or explicit fail-fast with no silent data loss.

### NFR-08: Cross-Process Concurrency

Multiple `python -m taskq` processes sharing one `$TASKQ_HOME` must not corrupt the three data files. Writes acquire an exclusive file lock and reads a shared lock using `fcntl.flock` on POSIX or `msvcrt.locking` on Windows. This is best-effort enhancement over atomic writes; on network filesystems the tool may downgrade to no flock while retaining atomic writes and emits `WARNING`. (SPEC §4, §11)

Acceptance criterion: four-process concurrent tests leave all three files valid with no corruption.

### NFR-09: Scalability

At 1000 tasks, combined `submit` and `status` p95, excluding subprocess execution, must be below 100 ms; the 100-iteration `<50ms` target remains covered by NFR-01. Running 100 tasks with `run --all` must leave valid `tasks.json` with no task loss, and peak memory must be below 100 MB using a streaming iterator rather than loading all tasks into memory. (SPEC §4, §11)

Acceptance criterion: scaled benchmark, integrity test, and memory test meet all thresholds.

### NFR-10: Schema Evolution

The root of all three data files must contain `version`, currently 1. Reading a version below 1 automatically upgrades to v1 and writes it back. Reading a version above 1 refuses access and prompts use of an upgrade tool. Before migration, the original is backed up as `<file>.v<n>.bak`; failed migration retains the backup and exits 1. (SPEC §4, §11)

Acceptance criterion: v0 fixtures migrate with backup; future versions refuse; failed migration preserves backup and exits 1.

## 5. Acceptance Criteria Summary

1. `pytest tests/ -q` is green.
2. Submit, run, and status happy path returns an 8-hex id, `done`, and `exit_code: 0`.
3. Empty and injection submissions exit 2.
4. A timeout configured with `TASKQ_TASK_TIMEOUT=1` yields `timeout` and exit 4.
5. Three consecutive final failures open the breaker; the next run exits 3; cooldown restores execution.
6. A TTL-valid cached run replays with `cached: true` and no subprocess.
7. `.env.example` declares all eight `TASKQ_*` variables.
8. Concurrent `run --all` leaves valid, lossless `tasks.json`.
9. Public function docstrings contain `[FR-XX]` references. (SPEC §8)
10. NFR benchmark, security, resilience, cross-process, scalability, and migration gates pass their stated measurable thresholds. (SPEC §§4, 11)

## 6. Out-of-Scope

- Runtime dependencies outside Python 3.11 standard library. (SPEC §1)
- Commands or capabilities not listed in FR-01 through FR-05, including an upgrade tool’s implementation details; NFR-10 only specifies refusal/prompt behavior for future versions. (SPEC §§3–4)
- Fault injection in normal production execution; it is test-only through the specified flag or monkeypatch. (SPEC §5.3)

## 7. Open Issues

- NFR-99: No unresolved TBD/TODO or placeholder requirement is present in canonical `SPEC.md` v4.0.0; no deferred FR is required. (SPEC §0)

## 8. Risks

- Concurrent writes may corrupt storage without locks and atomic replacement; mitigate with shared locking plus atomic writes (R1).
- Subprocess hangs or zombies may occur; mitigate with required timeout (R2).
- Breaker false-locking may deny work; mitigate with cooldown and HALF_OPEN (R3).
- Cache replay may be stale; mitigate with TTL expiration (R4).
- Secrets may reach disk; mitigate with stdout/stderr redaction (R5).
- Fault injection could interfere with normal tests; restrict it to explicit test paths (R6).
- Network filesystems may not support flock; retain atomic writes and issue a warning (R7).
- 1000-task scale may exceed memory; use streaming iteration (R8).
- Migration failure may cause data loss; retain `<file>.v<n>.bak` and fail fast (R9). (SPEC §9)

## 9. Glossary

| Term | Definition |
|---|---|
| `taskq` | Local task queue CLI tool. |
| Task | A submitted command with id, status, metadata, and execution result. |
| `TASKQ_HOME` | Directory containing the three persistent JSON data files. |
| `pending`, `running`, `done`, `failed`, `timeout` | Canonical task lifecycle states. |
| `OPEN`, `HALF_OPEN`, `CLOSED` | Circuit-breaker states. |
| TTL | Time-to-live interval for cached results. |
| p95 | 95th-percentile latency. |
| Atomic write | Temporary-file write followed by `os.replace`. |

## Machine-Readable Requirements

```json
{
  "version": "1.0",
  "created_at": "2026-07-28",
  "phase": 1,
  "project": "taskq",
  "functional_requirements": [
    {"id":"FR-01","description":"任務提交與驗證 per SPEC §3","implementation_functions":["taskq.cli","taskq.store"],"verification_method":"validation and persistence tests"},
    {"id":"FR-02","description":"任務執行器 per SPEC §3","implementation_functions":["taskq.executor","taskq.store"],"verification_method":"execution, timeout, concurrency tests"},
    {"id":"FR-03","description":"重試與斷路器 per SPEC §3","implementation_functions":["taskq.executor","taskq.breaker"],"verification_method":"retry and state-machine tests"},
    {"id":"FR-04","description":"結果 TTL 快取 per SPEC §3","implementation_functions":["taskq.cache","taskq.executor"],"verification_method":"TTL replay and atomicity tests"},
    {"id":"FR-05","description":"CLI 整合 per SPEC §3","implementation_functions":["taskq.__main__","taskq.cli"],"verification_method":"CLI integration and exit-code tests"}
  ],
  "non_functional_requirements": [
    {"id":"NFR-01","type":"performance","description":"submit+status p95 < 50ms over 100 iterations","test_method":"pytest-benchmark"},
    {"id":"NFR-02","type":"security","description":"no shell=True and injection coverage","test_method":"source scan and tests"},
    {"id":"NFR-03","type":"reliability","description":"atomic writes and breaker recovery bound","test_method":"fault and recovery tests"},
    {"id":"NFR-04","type":"security","description":"secret-line redaction before persistence","test_method":"redaction unit tests"},
    {"id":"NFR-05","type":"maintainability","description":"public docstrings contain FR references","test_method":"inspection"},
    {"id":"NFR-06","type":"deployability","description":"eight env vars and complete .env.example","test_method":"configuration tests"},
    {"id":"NFR-07","type":"resilience","description":"fault injection recovers or fails explicitly","test_method":"fault-injection tests"},
    {"id":"NFR-08","type":"concurrency","description":"cross-process file locking and integrity","test_method":"multi-process test"},
    {"id":"NFR-09","type":"scalability","description":"1000-task latency, 100-task losslessness, and memory bound","test_method":"scaled benchmark and integrity test"},
    {"id":"NFR-10","type":"evolvability","description":"version migration, backup, and future-version refusal","test_method":"migration fixture tests"}
  ]
}
```

Canonical citation for all requirements: `SPEC.md` v4.0.0, §§3–11.
