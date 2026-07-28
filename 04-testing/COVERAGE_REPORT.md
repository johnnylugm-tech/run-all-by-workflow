# COVERAGE_REPORT.md — `taskq` Phase 4 Coverage Report

**Project:** taskq (Local task-queue CLI)
**Measured:** 2026-07-28
**Interpreter:** `/Users/johnny/projects/run-all-by-workflow/.venv/bin/python` (CPython 3.11.15, darwin)
**Raw output:** `04-testing/coverage_raw.txt` (verbatim `pytest --cov` tee)
**Config:** `.coveragerc` (`branch = false`, `source = 03-development/src`, `skip_empty = true`, parallel + subprocess tracking)

---

## 1. Headline Number

- **Line coverage: 99%**
- Branch coverage: not measured (`branch = false` in `.coveragerc`)
- Gate 3 threshold: ≥ 80% → **PASS** (+19 pts margin)
- `advance-phase` ratchet: `--cov-fail-under=100` → **FAIL by 1 statement** (see §4)

Measurement commands (both re-run, identical results):

```bash
.venv/bin/python -m pytest --cov=03-development/src --cov-report=term-missing -q \
  | tee 04-testing/coverage_raw.txt
.venv/bin/python -m coverage report --format=total
# -> 99
```

---

## 2. Per-module Breakdown

Verbatim from `pytest --cov=03-development/src --cov-report=term-missing`:

| Module | Stmts | Miss | Cover | Missing lines |
|---|---:|---:|---:|---|
| `03-development/src/taskq/__init__.py` | 2 | 0 | 100% | — |
| `03-development/src/taskq/__main__.py` | 3 | 0 | 100% | — |
| `03-development/src/taskq/breaker.py` | 66 | 0 | 100% | — |
| `03-development/src/taskq/cache.py` | 38 | 0 | 100% | — |
| `03-development/src/taskq/cli.py` | 122 | 0 | 100% | — |
| `03-development/src/taskq/executor.py` | 88 | 0 | 100% | — |
| `03-development/src/taskq/store.py` | 34 | 1 | 97% | **63** |
| **TOTAL** | **353** | **1** | **99%** | — |

`2 empty files skipped` — `taskq/config.py` and `taskq/models.py` are docstring-only SAB
layer placeholders with zero executable statements, so `skip_empty = true` excludes them.
They contribute nothing to the denominator; this is not hidden uncovered code.

---

## 3. Coverage per FR Module

| FR | Implementing module | Coverage | Test file |
|---|---|---|---|
| FR-01 | `taskq.store` (+ `taskq.cli`) | 97% / 100% | `tests/test_fr01.py` |
| FR-02 | `taskq.executor` | 100% | `tests/test_fr02.py` |
| FR-03 | `taskq.breaker` | 100% | `tests/test_fr03.py` |
| FR-04 | `taskq.cache` | 100% | `tests/test_fr04.py` |
| FR-05 | `taskq.cli` | 100% | `tests/test_fr05.py` |

---

## 4. Uncovered Lines — Detail

### `03-development/src/taskq/store.py:63`

```python
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()   # <-- line 63, never executed
```

**Why it is uncovered:** the temp file only still exists inside `finally` when
`os.replace` (or the preceding `write_text`) raised. Every existing test takes the
success path, where `os.replace` has already renamed `tmp` away, so `tmp.exists()`
is `False` and `unlink()` is never reached.

**Impact:** low functional risk — the line is atomic-write cleanup, not business
logic. But it is the sole reason the repo sits at 99% instead of the 100% required
by the `advance-phase` `--cov-fail-under=100` ratchet.

**How to close it (not done in this doc-only step):** add a fault-injection test that
monkeypatches `os.replace` in `taskq.store` to raise `OSError`, asserts the exception
propagates, and asserts the `.tmp` file was removed from `$TASKQ_HOME`.

---

## 5. Verdict

| Check | Threshold | Actual | Result |
|---|---|---|---|
| Gate 3 line coverage | ≥ 80% | 99% | ✅ PASS |
| Per-FR module coverage | ≥ 80% each | 97–100% | ✅ PASS |
| `advance-phase` ratchet | 100% | 99% (1 miss) | ❌ FAIL — `store.py:63` |

All numbers in this document were read directly from live `pytest --cov` /
`coverage report --format=total` output; none are estimated.
