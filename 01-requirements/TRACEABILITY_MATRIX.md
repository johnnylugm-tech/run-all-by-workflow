# Traceability Matrix — taskq

> Requirements Traceability Matrix (RTM) for `taskq`, based on `SRS.md` v1.0 and canonical `SPEC.md` v4.0.0.
>
> **Lifecycle status:** Phase 1 requirements baseline. Design elements and test cases are planned trace points; implementation and execution evidence are not yet available. `SAD.md`, `TEST_SPEC.md`, `TEST_PLAN.md`, and `TEST_RESULTS.md` are downstream evidence sources.

## 1. Traceability policy and status

| Status | Meaning |
|---|---|
| `Defined` | The requirement and its acceptance boundary are present in `SRS.md`. |
| `Planned` | A design or test trace point is assigned, but downstream evidence is not yet produced. |
| `Verified` | Downstream design and test evidence demonstrates the acceptance criterion. |
| `Gap` | A required trace point or evidence link is missing. |

Traceability is maintained in both directions:

1. **Forward:** requirement → SRS acceptance boundary → design element → test case → result evidence.
2. **Backward:** design element and test case IDs map back to one or more requirement IDs; unreferenced elements or tests are reported as orphans.

At this phase, all requirements have a defined forward plan and no requirement is claimed as verified. The absence of `TEST_INVENTORY.yaml` is recorded as a pending downstream artifact rather than treated as test execution evidence.

## 2. Functional requirement matrix

| Requirement | SRS acceptance boundary | Design element(s) / planned owner | Planned test case(s) | Forward status |
|---|---|---|---|---|
| **FR-01** Submit and validate tasks | Empty/whitespace, >1000 chars, injection characters, and duplicate pending/running names reject with stderr, no storage write, exit 2. Valid input creates an 8-hex ID, `pending` task, metadata, atomic `tasks.json` write, and optional one-line JSON. (`SRS.md` §3 FR-01) | `taskq.cli.submit`; `taskq.models.Task`; `taskq.store` validation lookup and atomic persistence; `taskq.config` for `TASKQ_HOME`. Architecture trace: `SAD.md` FR-01 design. | `TC-FR01-01` empty/whitespace rejection; `TC-FR01-02` length rejection; `TC-FR01-03` injection blacklist; `TC-FR01-04` duplicate-name rejection; `TC-FR01-05` valid ID and pending record; `TC-FR01-06` atomic persistence; `TC-FR01-07` JSON output. | Defined → Planned |
| **FR-02** Execute tasks | `run <id>` and `run --all` use argument splitting with no `shell=True`; lifecycle is `pending → running → done/failed/timeout`; result tails are bounded to 2000 characters; timeout and concurrent execution behavior meet the SRS. (`SRS.md` §3 FR-02) | `taskq.executor.run_task` and `run_all`; `taskq.store` lifecycle/result writes and shared lock; `taskq.models.TaskResult`; `taskq.config` timeout/worker settings. Architecture trace: `SAD.md` FR-02 design. | `TC-FR02-01` successful command and result fields; `TC-FR02-02` non-zero failure; `TC-FR02-03` timeout and exit 4; `TC-FR02-04` stdout/stderr 2000-character tails; `TC-FR02-05` no `shell=True` and safe splitting; `TC-FR02-06` `run --all` concurrency; `TC-FR02-07` thread-safe, lossless task storage. | Defined → Planned |
| **FR-03** Retry and circuit breaker | Failed/timeout results retry up to configured limit with injectable exponential backoff. Consecutive final failures open the persistent global breaker; OPEN rejects with exit 3; cooldown permits one HALF_OPEN trial with required success/failure transitions. (`SRS.md` §3 FR-03) | `taskq.executor.retry`; `taskq.breaker.CircuitBreaker`; `taskq.store` atomic `breaker.json` persistence and lock; `taskq.config` retry/backoff/threshold/cooldown settings. Architecture trace: `SAD.md` FR-03 design. | `TC-FR03-01` retry cap; `TC-FR03-02` exponential delay and injected sleep; `TC-FR03-03` threshold opens breaker; `TC-FR03-04` OPEN rejection without subprocess; `TC-FR03-05` HALF_OPEN success closes/resets; `TC-FR03-06` HALF_OPEN failure reopens; `TC-FR03-07` cross-process persistent breaker state. | Defined → Planned |
| **FR-04** TTL result cache | SHA-256 command signature; recent `done` result within TTL replays with no subprocess and `cached: true`; missing/expired entries execute normally; successful results are atomically and thread-safely cached. (`SRS.md` §3 FR-04) | `taskq.cache.Cache`; `taskq.executor` cached-run branch; `taskq.store` lock coordination; `taskq.config` `TASKQ_CACHE_TTL`. Architecture trace: `SAD.md` FR-04 design. | `TC-FR04-01` SHA-256 signature; `TC-FR04-02` valid replay without subprocess; `TC-FR04-03` missing entry executes; `TC-FR04-04` expired entry executes; `TC-FR04-05` successful cache persistence; `TC-FR04-06` concurrent cache atomicity. | Defined → Planned |
| **FR-05** CLI integration | `python -m taskq` exposes submit/run/status/list/clear, optional filtering and global one-line JSON, with exit codes 0/1/2/3/4 and canonical unknown-task/breaker/timeout behavior. (`SRS.md` §3 FR-05) | `taskq.__main__` entry point; `taskq.cli` argparse parser, command dispatch, rendering and exit-code mapping; `taskq.store` status/list/clear operations. Architecture trace: `SAD.md` FR-05 design. | `TC-FR05-01` submit command; `TC-FR05-02` run modes and `--cached`; `TC-FR05-03` status all fields; `TC-FR05-04` list and status filter; `TC-FR05-05` clear data; `TC-FR05-06` one-line JSON; `TC-FR05-07` exit-code and error-message map; `TC-FR05-08` `python -m taskq` entry. | Defined → Planned |

## 3. Non-functional requirement matrix

| Requirement | SRS acceptance boundary | Design element(s) / planned owner | Planned test case(s) | Forward status |
|---|---|---|---|---|
| **NFR-01** Performance | Combined submit/status p95 is `<50 ms` over 100 iterations, excluding subprocess execution. (`SRS.md` §4 NFR-01) | `taskq.cli`, `taskq.store`, `taskq.config`; benchmark fixture and measurement boundary documented in `TEST_SPEC.md`. Architecture trace: `SAD.md` NFR-01. | `TC-NFR01-01` 100-iteration p95 benchmark. | Defined → Planned |
| **NFR-02** Security | Zero `shell=True` in codebase; FR-01 injection blacklist has coverage for every listed character. (`SRS.md` §4 NFR-02) | `taskq.executor` safe subprocess invocation; `taskq.cli` validation; source-scan check. Architecture trace: `SAD.md` NFR-02. | `TC-NFR02-01` source scan for `shell=True`; `TC-NFR02-02` blacklist character coverage. | Defined → Planned |
| **NFR-03** Reliability | `tasks.json`, `breaker.json`, and `cache.json` use temporary-file plus `os.replace`; files remain valid JSON after interruption; breaker recovery is no later than cooldown + 1s. (`SRS.md` §4 NFR-03) | `taskq.store.atomic_write`; `taskq.breaker`; `taskq.cache`; shared persistence/lock boundary. Architecture trace: `SAD.md` NFR-03. | `TC-NFR03-01` atomic write inspection; `TC-NFR03-02` interrupted-write JSON validity; `TC-NFR03-03` breaker recovery bound. | Defined → Planned |
| **NFR-04** Secret redaction | Before persistence, every line matching `sk-[A-Za-z0-9_-]{8,}` or `token=\S+` in output tails is replaced entirely with `[REDACTED]`. (`SRS.md` §4 NFR-04) | `taskq.models`/`taskq.store` persistence redaction boundary for `stdout_tail` and `stderr_tail`. Architecture trace: `SAD.md` NFR-04. | `TC-NFR04-01` matching-line stdout redaction; `TC-NFR04-02` matching-line stderr redaction; `TC-NFR04-03` non-matching line preservation. | Defined → Planned |
| **NFR-05** Maintainability | Every public function/class in `src/taskq` has a docstring containing an `[FR-XX]` reference. (`SRS.md` §4 NFR-05) | All public APIs in canonical modules; inspection rule recorded in `TEST_SPEC.md`. Architecture trace: `SAD.md` NFR-05. | `TC-NFR05-01` public callable docstring inspection and 100% coverage calculation. | Defined → Planned |
| **NFR-06** Deployability | `config.py` reads all eight `TASKQ_*` variables with canonical defaults; `.env.example` declares and annotates all eight. (`SRS.md` §4 NFR-06) | `taskq.config.Config`; `.env.example`; configuration loading boundary. Architecture trace: `SAD.md` NFR-06. | `TC-NFR06-01` all default values; `TC-NFR06-02` all environment overrides; `TC-NFR06-03` `.env.example` eight-variable check. | Defined → Planned |
| **NFR-07** Resilience | For each data file and each mid-write corruption, `OSError`, disk-full, and kill-during-write scenario, recover from backup or fail fast with explicit stderr/non-zero exit; no silent rebuild/swallowing. Fault injection is test-only. (`SRS.md` §4 NFR-07) | `taskq.store.atomic_write` recovery/fail-fast policy; fault-injection test seam; `taskq.breaker` and `taskq.cache` persistence paths. Architecture trace: `SAD.md` NFR-07. | `TC-NFR07-01` tasks fault scenarios; `TC-NFR07-02` breaker fault scenarios; `TC-NFR07-03` cache fault scenarios; `TC-NFR07-04` explicit error and exit behavior; `TC-NFR07-05` normal path does not enable injection. | Defined → Planned |
| **NFR-08** Cross-process concurrency | Multiple processes sharing `TASKQ_HOME` leave all three files valid; writes use exclusive and reads shared file locks where supported; network-filesystem downgrade retains atomic writes and emits `WARNING`. (`SRS.md` §4 NFR-08) | `taskq.store` platform lock adapter (`fcntl.flock`/`msvcrt.locking`) and atomic-write fallback; warning path. Architecture trace: `SAD.md` NFR-08. | `TC-NFR08-01` four-process concurrent integrity; `TC-NFR08-02` exclusive/shared lock behavior; `TC-NFR08-03` network-filesystem warning/fallback. | Defined → Planned |
| **NFR-09** Scalability | At 1000 tasks submit/status p95 is `<100 ms`; 100-task `run --all` is lossless and valid JSON; peak memory is `<100 MB` using streaming iteration. (`SRS.md` §4 NFR-09) | `taskq.store` streaming task iterator; `taskq.executor.run_all`; benchmark and memory fixtures. Architecture trace: `SAD.md` NFR-09. | `TC-NFR09-01` 1000-task p95 benchmark; `TC-NFR09-02` 100-task losslessness/JSON integrity; `TC-NFR09-03` peak-memory bound. | Defined → Planned |
| **NFR-10** Schema evolution | All roots contain `version: 1`; versions below 1 migrate and back up; versions above 1 refuse access and prompt upgrade; failed migration retains backup and exits 1. (`SRS.md` §4 NFR-10) | `taskq.store` schema reader/migrator, version gate, backup naming, and fail-fast path for all three files. Architecture trace: `SAD.md` NFR-10. | `TC-NFR10-01` v0-to-v1 migration and backup; `TC-NFR10-02` future-version refusal; `TC-NFR10-03` failed migration preserves backup and exits 1; `TC-NFR10-04` all three file types. | Defined → Planned |

## 4. Design-to-requirement reverse index

This index prevents design elements from becoming orphaned. Each planned element must remain linked to at least one requirement in `SAD.md` and this matrix.

| Design element / planned module | Requirement links | Downstream design evidence |
|---|---|---|
| `taskq.__main__` | FR-05 | `SAD.md` CLI entry design |
| `taskq.cli` argparse, validation, dispatch, rendering | FR-01, FR-05, NFR-02 | `SAD.md` CLI design |
| `taskq.config` environment configuration | FR-01, FR-02, FR-03, FR-04, NFR-06 | `SAD.md` configuration design |
| `taskq.models` task/result/schema models | FR-01, FR-02, FR-04, NFR-04, NFR-10 | `SAD.md` data model design |
| `taskq.store` JSON persistence, atomic writes, locks, migration | FR-01, FR-02, FR-03, FR-04, FR-05, NFR-03, NFR-07, NFR-08, NFR-09, NFR-10 | `SAD.md` storage design |
| `taskq.executor` subprocess, timeout, concurrency, retry | FR-02, FR-03, FR-04, NFR-01, NFR-02, NFR-09 | `SAD.md` execution design |
| `taskq.breaker` state machine and persistence | FR-03, NFR-03, NFR-07, NFR-08, NFR-10 | `SAD.md` breaker design |
| `taskq.cache` signature, TTL, persistence | FR-04, NFR-03, NFR-04, NFR-07, NFR-08, NFR-10 | `SAD.md` cache design |
| `.env.example` | NFR-06 | `SAD.md` deployment/configuration design |

**Reverse-index rule:** an element appearing in `SAD.md` but absent above is an orphan and is a traceability gap; an element above that is absent from `SAD.md` remains `Planned` until the architecture artifact is updated.

## 5. Test-to-requirement reverse index

The following IDs are the Phase 1 naming authority for `TEST_INVENTORY.yaml`; each ID must become one inventory entry and later map to a test specification and result. A test may cover multiple requirements only when every covered requirement is listed in its test record.

| Test ID range | Requirement(s) | Planned evidence |
|---|---|---|
| `TC-FR01-01..07` | FR-01 | `TEST_INVENTORY.yaml` → `TEST_SPEC.md` → `TEST_RESULTS.md` |
| `TC-FR02-01..07` | FR-02 | `TEST_INVENTORY.yaml` → `TEST_SPEC.md` → `TEST_RESULTS.md` |
| `TC-FR03-01..07` | FR-03 | `TEST_INVENTORY.yaml` → `TEST_SPEC.md` → `TEST_RESULTS.md` |
| `TC-FR04-01..06` | FR-04 | `TEST_INVENTORY.yaml` → `TEST_SPEC.md` → `TEST_RESULTS.md` |
| `TC-FR05-01..08` | FR-05 | `TEST_INVENTORY.yaml` → `TEST_SPEC.md` → `TEST_RESULTS.md` |
| `TC-NFR01-01` | NFR-01 | `TEST_INVENTORY.yaml` → `TEST_PLAN.md` → `TEST_RESULTS.md` |
| `TC-NFR02-01..02` | NFR-02 | `TEST_INVENTORY.yaml` → `TEST_PLAN.md` → `TEST_RESULTS.md` |
| `TC-NFR03-01..03` | NFR-03 | `TEST_INVENTORY.yaml` → `TEST_PLAN.md` → `TEST_RESULTS.md` |
| `TC-NFR04-01..03` | NFR-04 | `TEST_INVENTORY.yaml` → `TEST_PLAN.md` → `TEST_RESULTS.md` |
| `TC-NFR05-01` | NFR-05 | `TEST_INVENTORY.yaml` → `TEST_PLAN.md` → `TEST_RESULTS.md` |
| `TC-NFR06-01..03` | NFR-06 | `TEST_INVENTORY.yaml` → `TEST_PLAN.md` → `TEST_RESULTS.md` |
| `TC-NFR07-01..05` | NFR-07 | `TEST_INVENTORY.yaml` → `TEST_PLAN.md` → `TEST_RESULTS.md` |
| `TC-NFR08-01..03` | NFR-08 | `TEST_INVENTORY.yaml` → `TEST_PLAN.md` → `TEST_RESULTS.md` |
| `TC-NFR09-01..03` | NFR-09 | `TEST_INVENTORY.yaml` → `TEST_PLAN.md` → `TEST_RESULTS.md` |
| `TC-NFR10-01..04` | NFR-10 | `TEST_INVENTORY.yaml` → `TEST_PLAN.md` → `TEST_RESULTS.md` |

**Inventory control:** the ranges above expand to 63 individual IDs (not collapsed sub-cases). Until `TEST_INVENTORY.yaml` exists, the reverse links are planned and test execution coverage is `Pending`, not `0%` measured coverage.

## 6. Coverage validation

| Trace dimension | Calculation | Current result | Status |
|---|---|---:|---|
| Requirement → SRS | 15 requirements represented / 15 in `SRS.md` | 100% | Defined |
| FR → design element | 5 FRs with at least one planned module / 5 FRs | 100% | Planned |
| NFR → design element | 10 NFRs with at least one planned module / 10 NFRs | 100% | Planned |
| FR → test case | 5 FRs with assigned test IDs / 5 FRs | 100% planned | Planned |
| NFR → test case | 10 NFRs with assigned test IDs / 10 NFRs | 100% planned | Planned |
| Design element → requirement | 9 listed elements with requirement links / 9 listed elements | 100% planned | Planned |
| Test case → requirement | 63 planned IDs with reverse links / 63 planned IDs | 100% planned | Planned |
| Test inventory materialized | `TEST_INVENTORY.yaml` present and validated | Not available | Gap — downstream action |
| Test execution evidence | Results mapped to passing cases | Not available | Pending — downstream action |

Coverage percentages marked `planned` demonstrate assignment completeness only. They must not be promoted to verified coverage until `TEST_INVENTORY.yaml`, `TEST_SPEC.md`, `TEST_PLAN.md`, and `TEST_RESULTS.md` provide the corresponding evidence.

## 7. Traceability gates and unresolved gaps

### Required downstream checks

- `SAD.md` must preserve every FR-01–FR-05 and NFR-01–NFR-10 design link; no architecture element may be orphaned.
- `TEST_INVENTORY.yaml` must enumerate each of the 63 test IDs above as a separate entry and retain the requirement IDs.
- `TEST_SPEC.md` must define observable inputs, expected outputs, and pass/fail criteria for each inventory entry.
- `TEST_PLAN.md` must define performance, security, resilience, concurrency, scalability, and migration execution conditions.
- `TEST_RESULTS.md` must map each executed test ID to evidence and outcome; only then can rows become `Verified`.

### Current gaps

1. Implementation source and `SAD.md` are not yet available in Phase 1; design traces are planned module-level links.
2. `TEST_INVENTORY.yaml` is not yet available; the 63 test IDs are naming inputs, not execution evidence.
3. No test results exist; all requirement rows remain `Defined → Planned`.

## 8. Source authority

- Functional and non-functional requirement authority: `SRS.md` §§3–4.
- Canonical source authority: `SPEC.md` v4.0.0 §§3–11.
- FR ownership/status cross-check: `SPEC_TRACKING.md` §§Project Info, Specification Status, and Completeness Check.
- Architecture evidence: `SAD.md` (downstream, planned).
- Test inventory/specification/evidence: `TEST_INVENTORY.yaml`, `TEST_SPEC.md`, `TEST_PLAN.md`, and `TEST_RESULTS.md` (downstream, planned).
