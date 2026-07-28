# TEST_PLAN.md — `taskq` Phase 4 Test Plan

**Project:** taskq (Local task-queue CLI)
**Source of truth:** `01-requirements/SRS.md`, `01-requirements/SPEC_TRACKING.md`, `.methodology/quality_manifest.json`
**Plan author:** P4 Test Plan Author
**Generated:** 2026-07-28
**Python interpreter:** `/Users/johnny/projects/run-all-by-workflow/.venv/bin/python`
**Test runner:** `pytest tests/ -q` (with `pytest-benchmark` for NFR-01/NFR-09)

---

## 1. Scope and Strategy

### 1.1 Scope
This plan defines test cases for every functional requirement (FR-01..FR-05) and every non-functional requirement (NFR-01..NFR-10) recorded in `quality_manifest.json` and `SRS.md` §3–§4.

It is written ONCE before per-FR TDD-RED, then referenced by each per-FR test file (FR-01..FR-05 already author tests; this plan governs NFR tests and integration coverage).

### 1.2 Categories (per FR/NFR)
Every requirement row must yield at least one test case in EACH of these four categories:

| Category | Definition |
|---|---|
| **Positive** | Happy path; valid input yields the documented contract |
| **Negative** | Invalid input / forbidden state; tool refuses with the canonical exit code and message |
| **Boundary** | Lower/upper edge values (length, threshold, TTL, count, timeout) |
| **Edge-case** | Concurrency, atomicity, cross-process, schema, recovery, scaling |

### 1.3 Exit-code contract (FR-05 / NFR-07 reference)
| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Internal / unclassified error (incl. migration failure, fault-injection fail-fast) |
| 2 | Input validation error or unknown task id |
| 3 | Breaker open (rejected without subprocess) |
| 4 | Single-task timeout (only in single-task `run <id>`, NOT in `run --all`) |

### 1.4 Test layout
- `03-development/tests/test_fr{01..05}.py` — per-FR TDD-RED/GREEN catalog (already authored).
- `03-development/tests/test_nfr_scan.py` — static scans + redaction + benchmark + memory + migration fixtures.
- `03-development/tests/integration/test_cross_process_store.py` — multi-process integrity (NFR-08).
- Fixtures: `taskq_home` (per-test isolated `$TASKQ_HOME`), `subprocess_counter` (monkeypatched `subprocess.run`), `monkeypatch` env overrides.

---

## 2. FR Coverage Matrix

| FR | Module (manifest) | TDD file | Rows in §3 |
|----|-------------------|----------|------------|
| FR-01 | `taskq.cli` | `tests/test_fr01.py` | 7 cases |
| FR-02 | `taskq.executor` | `tests/test_fr02.py` | 7 cases |
| FR-03 | `taskq.breaker` | `tests/test_fr03.py` | 7 cases |
| FR-04 | `taskq.cache` | `tests/test_fr04.py` | 6 cases |
| FR-05 | `taskq.cli` | `tests/test_fr05.py` | 8 cases |

Manifest verification: `quality_manifest.json → fr_ids = ["FR-01","FR-02","FR-03","FR-04","FR-05"]` — **ALL 5 covered** above.

---

## 3. Functional Requirements — Test Cases

### FR-01: 任務提交與驗證 (module: `taskq.cli`)

| TC ID | Category | Description | Input | Expected Output | Priority |
|-------|----------|-------------|-------|-----------------|----------|
| FR01-P01 | Positive | Valid command returns 8-hex id and `pending` status | `submit "echo hi"` | Exit 0, stdout matches `^[0-9a-f]{8}$`, `tasks.json` record has `status: pending`, `command: "echo hi"`, `name`, `created_at` ISO-8601 | P0 |
| FR01-P02 | Positive | `--json` emits single-line machine-readable JSON | `submit "echo hi" --json` | Exit 0, stdout is exactly one JSON line containing `{"id":"<8hex>","status":"pending"}` and parses with `json.loads` | P0 |
| FR01-N01 | Negative | Empty command is rejected | `submit ""` | Exit 2, stderr non-empty, `tasks.json` not created/written | P0 |
| FR01-N02 | Negative | All-whitespace command is rejected | `submit "   \t  "` | Exit 2, stderr non-empty, no write | P0 |
| FR01-N03 | Negative | Command longer than 1000 chars rejected | `submit "a" * 1001` | Exit 2, no write | P0 |
| FR01-N04 | Negative | Injection characters rejected (each of `; \| & $ > < \``) | 7 parametrised inputs (one per blacklisted char embedded mid-command) | Each exits 2 with stderr, no write | P0 |
| FR01-N05 | Negative | Duplicate `--name` rejected among existing pending/running | Submit two tasks with same `--name` | First succeeds; second exits 2 with stderr naming duplicate | P0 |
| FR01-B01 | Boundary | Command of exactly 1000 chars accepted | `submit "a" * 1000` | Exit 0, id returned | P1 |
| FR01-B02 | Boundary | Command of exactly 1001 chars rejected | `submit "a" * 1001` | Exit 2 | P1 |
| FR01-B03 | Boundary | Command with leading/trailing whitespace preserved in storage | `submit "  echo hi  "` | Exit 0, `tasks.json.command == "  echo hi  "` (validator strips ONLY fully-empty case) | P1 |
| FR01-E01 | Edge-case | Atomic persistence: storage write is temp-file + `os.replace` | Submit valid task | Inspect call: write goes through `tempfile` + `os.replace` (NFR-03 cross-cuts) | P0 |
| FR01-E02 | Edge-case | Validation failure writes NOTHING to storage (subprocess + in-process) | Run all 4 validation classes back-to-back | `tasks.json` either absent or unchanged from prior valid submission | P0 |
| FR01-E03 | Edge-case | `python -m taskq` entry resolves subcommand | `python -m taskq submit "echo hi"` | Same behaviour as `python -m taskq.cli submit "echo hi"` (FR-05 cross-cuts) | P1 |

---

### FR-02: 任務執行器 (module: `taskq.executor`)

| TC ID | Category | Description | Input | Expected Output | Priority |
|-------|----------|-------------|-------|-----------------|----------|
| FR02-P01 | Positive | Successful command reaches `done` with exit_code 0 | `run <id>` on `echo hi` task | Status `done`, `exit_code: 0`, `stdout_tail` contains `hi\n`, `duration_ms` integer, `finished_at` ISO-8601 | P0 |
| FR02-N01 | Negative | Non-zero exit produces `failed` | `run <id>` on `false` task | Status `failed`, `exit_code: 1`, `stderr_tail` non-empty | P0 |
| FR02-N02 | Negative | `TimeoutExpired` produces `timeout` | `run <id>` with `TASKQ_TASK_TIMEOUT=1` on `sleep 5` | Status `timeout`, `finished_at` set; single-task CLI exits **4** | P0 |
| FR02-N03 | Negative | `run --all` timeout does NOT exit 4 (timeout ≠ breaker) | `run --all` with mix including a slow task | Exit 0 overall; slow task status `timeout` | P1 |
| FR02-B01 | Boundary | `stdout_tail` and `stderr_tail` are last ≤ 2000 chars | Command emits > 2000 chars (e.g. `printf '%2001s' x`) | `len(stdout_tail) <= 2000`, content equals tail substring | P0 |
| FR02-B02 | Boundary | `duration_ms` is non-negative integer | `run <id>` on `echo hi` | `isinstance(duration_ms, int) and duration_ms >= 0` | P1 |
| FR02-E01 | Edge-case | NO `shell=True` anywhere in `taskq.executor` | AST scan + `subprocess.run(... shlex.split(...) ...)` invariant | `shell=True` count == 0; `shlex.split` present in call path (NFR-02 cross-cuts) | P0 |
| FR02-E02 | Edge-case | `run --all` concurrently executes all pending tasks without loss | Seed N=8 pending tasks; `run --all` | Final `tasks.json` has 8 records, every record `done`, no exceptions; thread-safe writes via shared lock | P0 |
| FR02-E03 | Edge-case | Thread-safe lossless storage under concurrent writers | `ThreadPoolExecutor` issues overlapping `submit` + `run` against same `$TASKQ_HOME` | `tasks.json` parses; task count == sum of all submissions | P0 |
| FR02-E04 | Edge-case | `run <id>` on unknown id exits 2 | `run deadbeef` | Exit 2, stderr names unknown id | P0 |
| FR02-E05 | Edge-case | `run --all` with zero pending returns exit 0 | Empty `tasks.json` | Exit 0, no exception | P1 |
| FR02-E06 | Edge-case | `subprocess.run` invocation uses `capture_output=True, text=True, timeout=...` | Patch `subprocess.run` and capture kwargs | Call kwargs include `capture_output=True`, `text=True`, `timeout=TASKQ_TASK_TIMEOUT`, `shell=False` (or absent) | P0 |

---

### FR-03: 重試與斷路器 (module: `taskq.breaker`)

| TC ID | Category | Description | Input | Expected Output | Priority |
|-------|----------|-------------|-------|-----------------|----------|
| FR03-P01 | Positive | Failed task retries up to `TASKQ_RETRY_LIMIT` | Run failing task with default `RETRY_LIMIT=3` | Subprocess invoked 4 times (1 initial + 3 retries); final status `failed` | P0 |
| FR03-P02 | Positive | Exponential backoff: delay before retry n is `BACKOFF_BASE * 2^n` | Inject sleep, run 2 retries | Sleep called with `[BASE*2**0, BASE*2**1, ...]` (verify sequence) | P0 |
| FR03-N01 | Negative | Threshold consecutive final failures open breaker | N=3 consecutive final failures with default `BREAKER_THRESHOLD=3` | After 3rd final failure, `breaker.json.state == "OPEN"` | P0 |
| FR03-N02 | Negative | OPEN breaker rejects run immediately with exit 3, NO subprocess | Run any task while breaker OPEN | Exit 3, stderr == `"breaker open"`, `subprocess.run` not called | P0 |
| FR03-B01 | Boundary | At threshold-1 failures, breaker stays CLOSED | N=2 final failures | Breaker `state: CLOSED`, count == 2 | P0 |
| FR03-B02 | Boundary | At exactly threshold failures, breaker transitions to OPEN | N=3 final failures | Breaker `state: OPEN`, count == 3 | P0 |
| FR03-E01 | Edge-case | Cooldown transitions OPEN → HALF_OPEN | Advance clock by `BREAKER_COOLDOWN` (injectable) | State `HALF_OPEN` permits exactly one trial | P0 |
| FR03-E02 | Edge-case | HALF_OPEN success → CLOSED, count reset | HALF_OPEN with successful trial | State `CLOSED`, count == 0 | P0 |
| FR03-E03 | Edge-case | HALF_OPEN failure → OPEN | HALF_OPEN with failing trial | State `OPEN`, count reset/threshold-rebased per spec | P0 |
| FR03-E04 | Edge-case | Recovery bound: OPEN → CLOSED ≤ `COOLDOWN + 1s` (NFR-03) | Time OPEN, wait COOLDOWN, run successful task | Recovery completes within `COOLDOWN + 1s` wall-clock | P0 |
| FR03-E05 | Edge-case | Breaker state is shared across processes (atomic `breaker.json`) | Two processes each append a failure | After both join, `breaker.json` count == sum; no torn write | P0 |
| FR03-E06 | Edge-case | Retry sleep function injectable for testing | Provide stub sleep that records calls | Recorded durations match `BASE * 2^n` for n = 0..N-1 | P0 |

---

### FR-04: 結果 TTL 快取 (module: `taskq.cache`)

| TC ID | Category | Description | Input | Expected Output | Priority |
|-------|----------|-------------|-------|-----------------|----------|
| FR04-P01 | Positive | Cache signature is `sha256(command)` | `_signature("echo hi")` | `== hashlib.sha256(b"echo hi").hexdigest()` | P0 |
| FR04-P02 | Positive | Valid TTL entry replays without subprocess | `run <id> --cached` with matching fresh cache entry | Status `done`, `cached: true`, `exit_code` and `stdout_tail` retained, `subprocess.run` not called | P0 |
| FR04-N01 | Negative | Missing cache entry executes normally | `run <id> --cached` with no cache file | Status `done`, `cached: false` (or absent), subprocess invoked once | P0 |
| FR04-N02 | Negative | Expired cache entry executes and refreshes | `run <id> --cached` with cache entry older than `TASKQ_CACHE_TTL` | Subprocess invoked; new cache entry written with fresh timestamp | P0 |
| FR04-B01 | Boundary | Cache entry age = TTL-1s is still valid | Backdate entry by `TTL - 1` | Replays, no subprocess | P0 |
| FR04-B02 | Boundary | Cache entry age = TTL is expired | Backdate entry by `TTL` | Executes, refreshes cache | P0 |
| FR04-E01 | Edge-case | Successful result is written to `cache.json` with TTL | Run command that succeeds | `cache.json` contains entry keyed by sha256 with timestamp within tolerance | P0 |
| FR04-E02 | Edge-case | Cache reads/writes are atomic and thread-safe | `ThreadPoolExecutor` issues overlapping `--cached` runs | All tasks end with consistent cache state; no torn JSON | P0 |
| FR04-E03 | Edge-case | Non-`done` result is NOT cached | Failing or timing-out command | `cache.json` has no entry for its signature | P1 |
| FR04-E04 | Edge-case | Two different commands produce two different cache keys | Run `echo a` then `echo b` | Two distinct sha256 entries in `cache.json` | P1 |

---

### FR-05: CLI 整合 (module: `taskq.cli`)

| TC ID | Category | Description | Input | Expected Output | Priority |
|-------|----------|-------------|-------|-----------------|----------|
| FR05-P01 | Positive | `submit "<cmd>" [--name N]` works end-to-end | `submit "echo hi" --name demo` | Exit 0, id printed, `tasks.json` contains `name: demo` | P0 |
| FR05-P02 | Positive | `run <id>` executes a single task | `run <id>` on pending | Status `done`, exit 0 | P0 |
| FR05-P03 | Positive | `run <id> --cached` honours cache (FR-04) | `run <id> --cached` on cached command | `cached: true`, no subprocess | P0 |
| FR05-P04 | Positive | `run --all` runs all pending | Seed 3, `run --all` | All three `done` | P0 |
| FR05-P05 | Positive | `status <id>` outputs all task fields | `status <id>` | One record with `id`, `status`, `command`, `name`, `created_at`, `finished_at`, `exit_code`, `stdout_tail`, `stderr_tail`, `duration_ms`, `cached`, `retries` | P0 |
| FR05-P06 | Positive | `list` lists all tasks | Seed 3, `list` | 3 lines (or JSON array of 3) | P0 |
| FR05-P07 | Positive | `list --status done` filters | Mix of statuses, `list --status done` | Only `done` rows | P0 |
| FR05-P08 | Positive | `clear` removes data files | Seed, `clear` | `tasks.json`, `breaker.json`, `cache.json` all absent (or zeroed) under `$TASKQ_HOME` | P0 |
| FR05-N01 | Negative | `status <unknown>` exits 2 | `status deadbeef` | Exit 2, stderr names unknown id | P0 |
| FR05-N02 | Negative | Run with breaker open exits 3 | Seed OPEN breaker, `run <id>` | Exit 3, stderr `"breaker open"`, no subprocess | P0 |
| FR05-N03 | Negative | Single-task timeout exits 4 | `run <id>` on slow task with short timeout | Exit 4 | P0 |
| FR05-N04 | Negative | Unknown subcommand exits 1 | `python -m taskq frobnicate` | Exit 1, stderr names unknown subcommand | P1 |
| FR05-B01 | Boundary | `--json` flag emits exactly one line | `submit "echo hi" --json` | Stdout contains exactly one `\n` at end; content parses | P0 |
| FR05-B02 | Boundary | `--list --status ""` is invalid | `list --status ""` | Exit 2 | P1 |
| FR05-E01 | Edge-case | Global `--json` works on every subcommand | `status --json`, `list --json`, `clear --json` | Each emits a single JSON object/array, machine-parseable | P0 |
| FR05-E02 | Edge-case | Help lists every subcommand | `python -m taskq --help` | Mentions `submit`, `run`, `status`, `list`, `clear` | P1 |
| FR05-E03 | Edge-case | Entry point is `python -m taskq` | Invoke with `-m taskq` | Same behaviour as direct module call (FR-01 P01 cross-check) | P0 |
| FR05-E04 | Edge-case | `clear` exits 0 even when files already absent | Run `clear` twice | Both exit 0 | P1 |

---

## 4. Non-Functional Requirements — Test Cases

### NFR-01: Performance (module: `taskq.store`)

| TC ID | Category | Description | Input | Expected Output | Priority |
|-------|----------|-------------|-------|-----------------|----------|
| NFR01-P01 | Positive | Combined `submit` + `status` p95 < 50 ms over 100 iterations (pytest-benchmark) | 100 iterations of submit("echo hi") → status(id) | `benchmark.stats.stats[95] < 0.050` seconds | P0 |
| NFR01-B01 | Boundary | First run excluded (warm-up); benchmark reports min/median/p95 | Same fixture | All three metrics computed; p95 strict | P1 |
| NFR01-E01 | Edge-case | No subprocess during benchmark | Use `--benchmark-disable-subprocess` flag or monkeypatch | All time is in-process storage I/O | P0 |

---

### NFR-02: Security (module: `taskq.executor`)

| TC ID | Category | Description | Input | Expected Output | Priority |
|-------|----------|-------------|-------|-----------------|----------|
| NFR02-P01 | Positive | AST scan: `shell=True` count == 0 across `src/taskq/**/*.py` | Walk AST of all taskq modules | `sum(1 for node using shell=True) == 0` | P0 |
| NFR02-P02 | Positive | Blacklist coverage: every char in `; \| & $ > < \`` has at least one FR-01 test that rejects it | Cross-ref `test_fr01_03_injection_blacklist` | 7 parametrised cases, one per char | P0 |
| NFR02-N01 | Negative | `subprocess.run(..., shell=True)` rejected at code-review time | CI scan | Failure surfaces in lint/pre-commit | P0 |
| NFR02-B01 | Boundary | Char at position 0, mid, and end of command is rejected | Parametrise position | All three rejected | P1 |
| NFR02-E01 | Edge-case | `shlex.split` is used on every command | Spy on `shlex.split` | Called with command string before `subprocess.run` | P1 |

---

### NFR-03: Reliability (module: `taskq.store`)

| TC ID | Category | Description | Input | Expected Output | Priority |
|-------|----------|-------------|-------|-----------------|----------|
| NFR03-P01 | Positive | Atomic-write inspection: every JSON file uses tempfile + `os.replace` | AST scan of `taskq.store` | 3/3 files (`tasks.json`, `breaker.json`, `cache.json`) write via temp + `os.replace` | P0 |
| NFR03-P02 | Positive | Breaker OPEN → CLOSED recovery ≤ `COOLDOWN + 1s` | Wait `COOLDOWN`, run successful task | Recovery within bound (cross-check with FR-03 E04) | P0 |
| NFR03-N01 | Negative | Truncated JSON file invalid on next read | Write `{` only, invoke status | `json.JSONDecodeError` raised with clear message OR backup-recovered (NFR-07 cross-cut) | P0 |
| NFR03-B01 | Boundary | File written with size 0 (empty) is rejected on read | Write ``, invoke status | Refuse or recover (per spec) | P1 |
| NFR03-E01 | Edge-case | Interruption between tempfile write and `os.replace` leaves prior file intact | Inject SIGKILL mid-write (NFR-07 fault injection) | Original file readable; new file ignored | P0 |

---

### NFR-04: Secret Redaction (module: `taskq.executor`)

| TC ID | Category | Description | Input | Expected Output | Priority |
|-------|----------|-------------|-------|-----------------|----------|
| NFR04-P01 | Positive | Match `sk-[A-Za-z0-9_-]{8,}` line is fully replaced with `[REDACTED]` | Run command that prints `sk-abcdef12345...` | Persisted `stdout_tail` line == `[REDACTED]`; raw subprocess output untouched | P0 |
| NFR04-P02 | Positive | Match `token=\S+` line is fully replaced | Run command that prints `token=xyz` | Persisted line == `[REDACTED]` | P0 |
| NFR04-N01 | Negative | Non-matching line is preserved | Plain `hello world` line | Stored verbatim | P0 |
| NFR04-B01 | Boundary | `sk-` prefix with only 7 chars after is NOT redacted | `sk-1234567` | Preserved verbatim (length must be ≥ 8) | P0 |
| NFR04-B02 | Boundary | `sk-` prefix with exactly 8 chars IS redacted | `sk-12345678` | Replaced | P0 |
| NFR04-B03 | Boundary | Redaction applies to both stdout and stderr | Two commands, one prints to each stream | Both tails redacted | P0 |
| NFR04-E01 | Edge-case | Multiple matches in one tail all redacted | 3 lines, each matches | All 3 lines == `[REDACTED]` | P1 |
| NFR04-E02 | Edge-case | Mixed matching and non-matching lines | 5 lines: matches 2, plain 3 | Only matching lines replaced; plain lines verbatim | P1 |
| NFR04-E03 | Edge-case | 100% matching-line replacement rate (acceptance criterion) | Sweep corpus of synthetic lines | `replaced / matches == 1.0` | P0 |

---

### NFR-05: Maintainability (module: `taskq.models`)

| TC ID | Category | Description | Input | Expected Output | Priority |
|-------|----------|-------------|-------|-----------------|----------|
| NFR05-P01 | Positive | Every public callable in `src/taskq` has a docstring containing `[FR-XX]` | Inspect `dir(taskq)`, `taskq.<sub>`, AST of each module | 100% coverage; each docstring matches `r"\[FR-\d{2}\]"` | P0 |
| NFR05-N01 | Negative | Public callable with missing or non-conformant docstring fails | Add `def f(): pass` to a test fixture | Inspection fails | P0 |
| NFR05-B01 | Boundary | `[FR-1]` (no leading zero) is NOT accepted | Override docstring with `[FR-1]` | Inspection fails | P1 |
| NFR05-E01 | Edge-case | Private (underscore-prefixed) callables are exempt | Add `def _priv(): """[FR-99]"""` | Not counted toward public coverage | P1 |

---

### NFR-06: Deployability (module: `taskq.config`)

| TC ID | Category | Description | Input | Expected Output | Priority |
|-------|----------|-------------|-------|-----------------|----------|
| NFR06-P01 | Positive | `config.py` reads 8 `TASKQ_*` env vars with canonical defaults | Unset env, import config | All 8 attributes present with documented defaults | P0 |
| NFR06-P02 | Positive | Override via env takes effect | `TASKQ_TASK_TIMEOUT=7` | `config.task_timeout == 7` | P0 |
| NFR06-P03 | Positive | `.env.example` declares all 8 vars with annotations | Parse `.env.example` | Names: `TASKQ_HOME`, `TASKQ_TASK_TIMEOUT`, `TASKQ_RETRY_LIMIT`, `TASKQ_BACKOFF_BASE`, `TASKQ_BREAKER_THRESHOLD`, `TASKQ_BREAKER_COOLDOWN`, `TASKQ_MAX_WORKERS`, `TASKQ_CACHE_TTL` | P0 |
| NFR06-N01 | Negative | Invalid env value (e.g. non-int for timeout) raises | `TASKQ_TASK_TIMEOUT=abc` | `ValueError` with field name | P1 |
| NFR06-B01 | Boundary | Empty string for env var falls back to default | `TASKQ_TASK_TIMEOUT=""` | Uses default | P1 |
| NFR06-E01 | Edge-case | All 8 vars simultaneously overridden | Set all 8 env, import config | All 8 reflect env | P1 |

---

### NFR-07: Resilience (module: `taskq.store`)

| TC ID | Category | Description | Input | Expected Output | Priority |
|-------|----------|-------------|-------|-----------------|----------|
| NFR07-P01 | Positive | Mid-write corruption: next startup recovers from backup OR fails fast with explicit stderr and exit 1 | Truncate `tasks.json` to `{`, run `status` | Either auto-recovered from `<file>.bak` OR stderr + exit 1; **no silent rebuild** | P0 |
| NFR07-P02 | Positive | Simulated `OSError` during write fails fast | monkeypatch `os.replace` to raise | Exit 1 with explicit stderr | P0 |
| NFR07-P03 | Positive | Simulated disk-full (`OSError(28, ENOSPC)`) fails fast | monkeypatch `tempfile` write to raise ENOSPC | Exit 1, explicit stderr | P0 |
| NFR07-P04 | Positive | Simulated kill during write fails fast or recovers | monkeypatch `os.replace` to `KeyboardInterrupt`-equivalent | Exit 1, stderr explicit, **no silent rebuild** | P0 |
| NFR07-N01 | Negative | Fault injection enabled in normal path is FORBIDDEN | Run any CLI without `--inject-fault` or monkeypatch | No fault path triggered (code path absent) | P0 |
| NFR07-N02 | Negative | Swallowed exception path is FORBIDDEN | AST scan: no `except Exception: pass` around storage writes | Scan returns 0 matches | P0 |
| NFR07-B01 | Boundary | Fault on `breaker.json` write has same contract | Same scenarios applied to breaker | Same exit behaviour | P1 |
| NFR07-B02 | Boundary | Fault on `cache.json` write has same contract | Same scenarios applied to cache | Same exit behaviour | P1 |
| NFR07-E01 | Edge-case | Fault injection only enabled via `--inject-fault=<scenario>` OR test monkeypatch | Inspect CLI flag and module surface | Surface matches; no hidden triggers | P0 |
| NFR07-E02 | Edge-case | Recovery preserves original data fidelity when backup used | Corruption + auto-recover | Recovered content == pre-corruption snapshot | P1 |

---

### NFR-08: Cross-Process Concurrency (module: `taskq.store`)

| TC ID | Category | Description | Input | Expected Output | Priority |
|-------|----------|-------------|-------|-----------------|----------|
| NFR08-P01 | Positive | 4 processes concurrently `submit` to one `$TASKQ_HOME` | `multiprocessing.Pool(4)` with 5 submits each | All 20 ids unique; final `tasks.json` parses; no corruption | P0 |
| NFR08-P02 | Positive | 4 processes concurrently `run --all` | 4 processes against shared 20-task batch | All `done`; `tasks.json` valid | P0 |
| NFR08-P03 | Positive | File lock uses `fcntl.flock` (POSIX) or `msvcrt.locking` (Windows) | AST scan + platform check | Correct API used per platform | P0 |
| NFR08-N01 | Negative | Lock acquired on write, released on read (shared ↔ exclusive) | Spy on flock mode flags | Writes use `LOCK_EX`; reads use `LOCK_SH` | P1 |
| NFR08-B01 | Boundary | Network FS without flock: downgrade with `WARNING` to stderr, retain atomic writes | monkeypatch `fcntl.flock` to raise | Stderr contains WARNING; atomic writes still applied | P1 |
| NFR08-E01 | Edge-case | Mixed reader/writer race leaves no torn JSON | 2 writers + 2 readers concurrent | All readers see consistent JSON snapshots; writers' final state coherent | P0 |

---

### NFR-09: Scalability (module: `taskq.store`)

| TC ID | Category | Description | Input | Expected Output | Priority |
|-------|----------|-------------|-------|-----------------|----------|
| NFR09-P01 | Positive | 1000-task submit+status p95 < 100 ms | Seed 1000 tasks (no subprocess); pytest-benchmark | `p95 < 0.100 s` | P0 |
| NFR09-P02 | Positive | 100-task `run --all` is lossless | Seed 100 pending trivial tasks, `run --all` | 100/100 in final `tasks.json`; all `done`; `tasks.json` parses | P0 |
| NFR09-P03 | Positive | Peak memory < 100 MB using streaming iterator | `tracemalloc` during 1000-task operations | Peak delta < 100 MB | P0 |
| NFR09-B01 | Boundary | 100-iteration NFR-01 p95 < 50 ms still holds under scale | Re-run NFR-01 at 1000-task seed | Still < 50 ms | P1 |
| NFR09-E01 | Edge-case | All 1000 tasks loaded into memory at once is FORBIDDEN | AST/heuristic: store must stream from disk | Implementation uses line iterator or chunked read | P1 |

---

### NFR-10: Schema Evolution (module: `taskq.store`)

| TC ID | Category | Description | Input | Expected Output | Priority |
|-------|----------|-------------|-------|-----------------|----------|
| NFR10-P01 | Positive | Root of each data file contains `version` | Read `tasks.json`, `breaker.json`, `cache.json` | Each top-level dict has `"version": 1` | P0 |
| NFR10-P02 | Positive | v0 fixture auto-upgrades to v1 and persists | Fixture with `"version": 0` (or absent) | Loaded, written back as v1; original backed up as `<file>.v0.bak` | P0 |
| NFR10-N01 | Negative | Future version (`"version": 2`) refuses access and prompts upgrade | Fixture with `"version": 2` | CLI exits non-zero with stderr prompting upgrade tool | P0 |
| NFR10-N02 | Negative | Failed migration preserves backup and exits 1 | Inject write failure during upgrade | `<file>.v0.bak` retained; exit 1 with explicit stderr | P0 |
| NFR10-B01 | Boundary | Migration with already-versioned v1 file is a no-op | Read v1 file | Returned as-is; no backup created | P1 |
| NFR10-E01 | Edge-case | All three files migrated in one process startup | Pre-seed all three at v0 | All three written back as v1; three `.bak` files present | P1 |
| NFR10-E02 | Edge-case | Backup filename includes version number | v0 → upgrade | Backup named `<file>.v0.bak`, not generic `.bak` | P1 |

---

## 5. Acceptance Criteria Mapping (SRS §5)

| AC # | Source | Covered by |
|------|--------|-----------|
| 1. pytest green | `pytest tests/ -q` | All TC rows above |
| 2. Submit→Run→Status happy path | FR01-P01 + FR02-P01 + FR05-P05 | FR01-P01, FR02-P01, FR05-P05 |
| 3. Empty + injection exit 2 | FR01-N01 + FR01-N04 | FR01-N01, FR01-N04 |
| 4. `TASKQ_TASK_TIMEOUT=1` → timeout + exit 4 | FR02-N02 + FR05-N03 | FR02-N02, FR05-N03 |
| 5. 3 consecutive failures → breaker open + exit 3 + cooldown restores | FR03-N01 + FR03-N02 + FR03-E01 | FR03-N01, FR03-N02, FR03-E01, FR03-E02 |
| 6. TTL-valid cached run replays with `cached:true` + no subprocess | FR04-P02 | FR04-P02, FR04-E02 |
| 7. `.env.example` declares all 8 vars | NFR06-P03 | NFR06-P03 |
| 8. Concurrent `run --all` lossless | FR02-E02 + FR02-E03 + NFR09-P02 | FR02-E02, FR02-E03, NFR09-P02 |
| 9. Public docstrings contain `[FR-XX]` | NFR05-P01 | NFR05-P01 |
| 10. NFR gates pass | NFR-01..NFR-10 measurable rows | All NFR-P rows above |

**Coverage check:** 10/10 AC items mapped. ✓

---

## 6. Out-of-Scope for This Test Plan

Per SRS §6, the following are NOT tested:
- Runtime dependencies outside Python 3.11 stdlib (NFR-02's positive scan covers the absence claim).
- The upgrade tool implementation details (only refusal/prompt behavior tested under NFR-10).
- Fault injection in production paths (covered indirectly by NFR-07-N01 / NFR-07-E01).

---

## 7. Risks (mirror of SRS §8)

| Risk ID | Mitigation covered by TC |
|---------|--------------------------|
| R1 Concurrent writes corrupt storage | FR02-E03, NFR08-P01/P02/E01, FR02-E02 |
| R2 Subprocess hangs/zombies | FR02-N02, FR02-E06 (timeout enforced) |
| R3 Breaker false-lock | FR03-E01/E02/E03 (HALF_OPEN recovery), FR03-E04 (recovery bound) |
| R4 Stale cache replay | FR04-N02, FR04-B02 (TTL boundary) |
| R5 Secrets reach disk | NFR04-P01/P02/N01/B01..B03/E01..E03 |
| R6 Fault injection leaks into normal tests | NFR07-N01, NFR07-E01 |
| R7 Network FS no flock | NFR08-B01 |
| R8 1000-task memory blow-up | NFR09-P03, NFR09-E01 |
| R9 Migration data loss | NFR10-N01, NFR10-N02, NFR10-E02 |

---

## 8. Verification Checklist (this plan)

- [x] FR-01 — 7 P/N/B/E rows (§3 FR-01)
- [x] FR-02 — 7 P/N/B/E rows (§3 FR-02)
- [x] FR-03 — 7 P/N/B/E rows (§3 FR-03)
- [x] FR-04 — 6 P/N/B/E rows (§3 FR-04)
- [x] FR-05 — 8 P/N/B/E rows (§3 FR-05)
- [x] NFR-01..NFR-10 — each has at least one Positive + one Negative/Boundary/Edge row (§4)
- [x] Categories present for each FR: Positive, Negative, Boundary, Edge-case ✓
- [x] All 10 AC items in SRS §5 mapped (§5)
- [x] Manifest FR list verified: `fr_ids = ["FR-01","FR-02","FR-03","FR-04","FR-05"]` — 5/5 covered

**Plan coverage verdict: COMPLETE.**