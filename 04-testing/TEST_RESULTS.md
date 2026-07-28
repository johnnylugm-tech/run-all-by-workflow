# TEST_RESULTS.md — `taskq` Phase 4 Test Execution Results

**Project:** taskq (Local task-queue CLI)
**Plan:** `04-testing/TEST_PLAN.md`
**Executed:** 2026-07-28
**Interpreter:** `/Users/johnny/projects/run-all-by-workflow/.venv/bin/python` (CPython 3.11.15, darwin)
**Runner:** `pytest` (raw output archived in `04-testing/coverage_raw.txt`)

---

## 1. Commands Executed

| # | Command | Purpose |
|---|---------|---------|
| 1 | `python -m pytest 03-development/tests -q` | Project suite only (FR + NFR + integration) |
| 2 | `python -m pytest --cov=03-development/src --cov-report=term-missing -q` | Full repo suite + coverage measurement |
| 3 | `python -m coverage report --format=total` | Machine-readable total (`99`) |

---

## 2. Result Summary

### 2.1 Project suite (`03-development/tests`)

```
58 passed, 1 skipped in 7.69s
```

| Metric | Value |
|---|---|
| Cases collected | 59 |
| Passed | 58 |
| Failed | **0** |
| Skipped | 1 |
| Errors | 0 |
| Wall time | 7.69 s |

### 2.2 Full repository suite (project + harness self-tests)

```
1 failed, 6254 passed, 1 skipped, 5 warnings in 90.22s
```

The single failure is in `harness/tests/` (harness self-test), not in `03-development/`. See §5.

---

## 3. Per-file Breakdown (project suite)

| Test file | Requirement | Collected | Passed | Skipped | Failed |
|---|---|---|---|---|---|
| `tests/test_fr01.py` | FR-01 (task submit / store) | 15 | 15 | 0 | 0 |
| `tests/test_fr02.py` | FR-02 (executor, concurrency) | 10 | 10 | 0 | 0 |
| `tests/test_fr03.py` | FR-03 (circuit breaker) | 7 | 7 | 0 | 0 |
| `tests/test_fr04.py` | FR-04 (result cache / TTL) | 6 | 6 | 0 | 0 |
| `tests/test_fr05.py` | FR-05 (CLI exit-code contract) | 8 | 8 | 0 | 0 |
| `tests/test_nfr_scan.py` | NFR-01..NFR-10 scans | 8 | 7 | 1 | 0 |
| `tests/integration/test_cross_process_store.py` | NFR-08 (cross-process integrity) | 5 | 5 | 0 | 0 |
| **Total** | — | **59** | **58** | **1** | **0** |

---

## 4. Requirement Status

| FR | Module under test | Tests | Status |
|---|---|---|---|
| FR-01 | `taskq.cli`, `taskq.store` | 15 | ✅ PASS |
| FR-02 | `taskq.executor` | 10 | ✅ PASS |
| FR-03 | `taskq.breaker` | 7 | ✅ PASS |
| FR-04 | `taskq.cache` | 6 | ✅ PASS |
| FR-05 | `taskq.cli` | 8 | ✅ PASS |

All five FR IDs registered in `.methodology/quality_manifest.json` have executed, passing test cases.
NFR-01..NFR-10 are covered by `test_nfr_scan.py` (7 passed) plus the NFR-08 integration suite (5 passed).

---

## 5. Deferred / Open Issues

| # | Item | Evidence | Severity | Disposition |
|---|---|---|---|---|
| D-1 | `harness/tests/test_file_size_ratchet.py::test_production_file_line_ratchet` fails: `cli/project_cmds.py: 2036 lines > ceiling 1986` | `coverage_raw.txt` FAILURES section | Medium | **Deferred — out of Phase-4 scope.** The file belongs to `harness/`, which this phase is forbidden to modify. Requires a harness-side decision (split the file or raise the ratchet ceiling). No `taskq` production code is involved. |
| D-2 | 1 skipped test: `test_nfr_scan.py:98` — *"no `.env.example` shipped — contract not yet instantiated by the project"* | `pytest -rs` output | Low | **Deferred.** Conditional skip by design; the assertion activates once an `.env.example` exists. Not a regression. |
| D-3 | Line coverage is 99%, one statement short of the `--cov-fail-under=100` threshold enforced by `advance-phase`. Uncovered: `03-development/src/taskq/store.py:63` (`tmp.unlink()` in the atomic-write `finally` cleanup). | `COVERAGE_REPORT.md` §3 | **High (blocks advance-phase, not Gate 3)** | **Open.** Gate 3 requires ≥80% and is satisfied; the 100% ratchet is not. Needs a fault-injection test that makes `os.replace` raise so the `finally` branch executes. Not authored here — Phase-4 coverage authoring is doc-only scope. |

---

## 6. Reproduction

```bash
cd /Users/johnny/projects/run-all-by-workflow
.venv/bin/python -m pytest 03-development/tests -q -rs
.venv/bin/python -m pytest --cov=03-development/src --cov-report=term-missing -q \
  | tee 04-testing/coverage_raw.txt
.venv/bin/python -m coverage report --format=total   # -> 99
```

Both coverage runs above were executed twice and produced identical numbers
(353 statements, 1 miss, 99%), so the result is stable, not flaky.
