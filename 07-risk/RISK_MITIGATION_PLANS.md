# Risk Mitigation Plans — run-all-by-workflow (`taskq` v0.1.0)

> **Phase**: 7 — Risk Management
> **Date**: 2026-07-28
> **Scope**: every risk whose *current* score (likelihood × impact) is **≥ 9** in
> [`RISK_REGISTER.md`](RISK_REGISTER.md) §3.
> **Framework**: harness-methodology v2.12.0

---

## 0. Plans in Scope

| Plan | Risk | Current score | Owner (role) | Target | Blocking? |
|------|------|---------------|--------------|--------|-----------|
| MP-01 | R9 — schema migration failure / data loss (NFR-10) | 2×5 = **10** | Agent A — Development (P9 maintenance author) | P9 exit | No (Gate 4 already PASS) |
| MP-02 | R11 — NFR-07 fault recovery unproven for `breaker.json` / `cache.json` | 3×3 = **9** | Agent A — Development (P9 maintenance author) | P9 exit | No |
| MP-03 | R13 — harness file-size ratchet failure (D-1) | 5×2 = **10** | harness-methodology maintainer (external; escalation via TECH_LEAD) | Next harness release | No for `taskq`; yes for harness CI |

MEDIUM risks (R7, R8, R10, R12, R14, R15) do **not** receive a formal plan here;
their handling is recorded as watch/accept items in
[`RISK_STATUS_REPORT.md`](RISK_STATUS_REPORT.md) §4.

> **Deadline semantics**: targets are anchored to *pipeline phase exits*, not to
> calendar commitments — no human owner has committed to a date. Calendar dates
> shown are the projected phase-exit dates given the current pipeline velocity
> (P7 entered 2026-07-28) and are explicitly marked **projected**.

---

## MP-01 — Prove the NFR-10 schema-migration safety net (R9)

### Statement

`taskq.store.load_store` performs a lazy v0→v1 upgrade. NFR-10 additionally
requires that the original file is backed up as `<file>.v<n>.bak` *before*
migrating, that a failed migration keeps the backup and exits 1 fail-fast, and
that `version > 1` is rejected with an upgrade hint. Two tests that would prove
this are skipped:

- `03-development/tests/test_nfr4_fault.py:280` — *"NFR-10 v0->v1 schema migration + .v\<n\>.bak deferred to P5"*
- `03-development/tests/test_nfr4_fault.py:287` — *"NFR-10 failed-migration path depends on migration wiring deferred to P5"*

Impact is 5 because the failure mode is silent destruction of the user's task
history. Likelihood is 2 because v1 is the only version in the wild today, so
the migration branch is rarely entered.

### Why this ranks above the alternatives

Two options were compared:

1. **Accept and document** (zero cost). Rejected: the accepted state would be
   "data-loss branch has no executing test", which contradicts the project rule
   that mitigations must be verifiable, and NFR-10 is claimed PASS in
   `FINAL_SIGN_OFF.md` §4 on the strength of the lazy upgrade alone.
2. **Un-skip and close the gap with behavioural tests** (chosen). Cost is
   bounded — the tests already exist as stubs; only the backup + fail-fast
   assertions and the wiring need writing.

### Actions

| # | Action | Verification |
|---|--------|--------------|
| 1 | Write a failing test: seed `$TASKQ_HOME/tasks.json` with `{"version": 0, ...}`, call `store.load_store`, assert `tasks.json.v0.bak` exists and its bytes equal the pre-migration content | TDD-RED — test fails on the current tree |
| 2 | Write a failing test: monkeypatch the migration writer to raise `OSError` mid-write; assert the `.v0.bak` survives, `stderr` names the file, and the process exits 1 | TDD-RED |
| 3 | Write a failing test: seed `{"version": 2}`, assert read is refused with an explicit upgrade message (no silent rebuild) | TDD-RED |
| 4 | Implement/complete backup + fail-fast in `taskq.store` until all three pass | TDD-GREEN — `pytest 03-development/tests/test_nfr4_fault.py -k nfr10` all PASS, 0 skipped |
| 5 | Remove the three `pytest.skip` calls | `grep -c "pytest.skip" test_nfr4_fault.py` decreases by 3 |
| 6 | Re-run the per-FR gate for the touched FR | Gate 1 PASS at ≥ current score; `pytest --cov=03-development/src --cov-fail-under=100` still green |

### Owner and target

- **Owner**: Agent A — Development role, executed as a P9 maintenance TDD chain
  (`run-fr-step --step TDD-RED → GREEN → IMPROVE → GATE1`).
- **Target**: P9 exit — **projected 2026-07-30**.
- **Escalation**: if 3 TDD rounds fail, escalate to TECH_LEAD with the pytest log,
  per the standard Gate 1 CASE-3 rule.

### Residual risk after completion

L 1 × I 5 = 5 (LOW). Impact cannot be reduced below 5 — the failure mode is
inherently data loss; only likelihood is addressable.

---

## MP-02 — Extend NFR-07 fault-injection coverage to all three data files (R11)

### Statement

NFR-07 requires that `tasks.json`, `breaker.json`, and `cache.json` each either
auto-recover or fail fast under mid-write corruption — never silently rebuild
and never silently swallow the error. Only the `tasks.json` path is proven.
Three skips mark the gap:

- `test_nfr4_fault.py:189` — *"NFR-07 breaker.json fault recovery deferred to P5 hardening"*
- `test_nfr4_fault.py:195` — *"NFR-07 cache.json fault recovery deferred to P5 hardening"*
- `test_nfr4_fault.py:200` — *"NFR-07 explicit stderr semantics rely on full migration story deferred to P5"*

A fourth skip at `:174` (`disk_full` simulation) is environment-conditional and
is **not** in scope — a conditional skip on a non-portable syscall is a
legitimate guard, not a gap.

### Why this ranks above the alternatives

1. **Rely on symmetry** — argue the three files share `_atomic_write_json`, so
   proving one proves all. Rejected: `breaker.json` and `cache.json` have
   different *recovery* semantics (a corrupted breaker state can legitimately be
   reset; a corrupted task store cannot), and the "never silently rebuild"
   clause is precisely the behaviour that differs per file.
2. **Parameterise the existing `tasks.json` fault test over all three files**
   (chosen). Lowest-code option: one parameterised fixture replaces three stubs,
   with a per-file expectation table for recover-vs-fail-fast.

### Actions

| # | Action | Verification |
|---|--------|--------------|
| 1 | Define the expected behaviour per file (recover / fail-fast) and record it in the test as an explicit table | Table is reviewable in the diff |
| 2 | Parameterise the corruption fixture over `tasks.json`, `breaker.json`, `cache.json` | TDD-RED — the two new params fail |
| 3 | Assert on `stderr` content and exit code for the fail-fast files; assert on restored content for the recovering files | TDD-RED |
| 4 | Implement the missing handling in `taskq.breaker` / `taskq.cache` | TDD-GREEN — all params PASS |
| 5 | Remove the three `pytest.skip` calls | skip count drops by 3 |
| 6 | Confirm no silent-swallow regression | `error_handling` dimension stays 100.0 (0 `broad_swallow` / `bare_except`) |

### Owner and target

- **Owner**: Agent A — Development role, P9 maintenance TDD chain.
- **Target**: P9 exit — **projected 2026-07-30**.
- **Escalation**: TECH_LEAD after 3 failed TDD rounds.

### Residual risk after completion

L 1 × I 3 = 3 (LOW).

---

## MP-03 — Escalate the harness file-size ratchet failure (R13)

### Statement

`harness/tests/test_file_size_ratchet.py::test_production_file_line_ratchet`
fails: `harness/cli/project_cmds.py` is 2036 lines against a ceiling of 1986.
This is recorded as D-1 (MEDIUM) in `05-verification/BASELINE.md` §5. Likelihood
is 5 — the test is failing right now. Impact is 2 — it degrades harness CI
signal and can mask a future regression, but no `taskq` production code is
involved.

### Why this is an escalation, not a fix

HR-17 forbids this project from modifying anything under `harness/`; the
submodule must be debugged upstream, never hot-patched. Both candidate remedies
(split `project_cmds.py`, or raise the ratchet ceiling with justification) are
harness-side decisions. Attempting either from here would violate the hard rule
and would be reverted on the next submodule sync.

### Actions

| # | Action | Verification |
|---|--------|--------------|
| 1 | File the finding upstream with the exact numbers (2036 vs 1986), the failing test id, and the observing project/commit (`7a691d1`) | Issue exists in the harness tracker |
| 2 | State the two remedies and let the harness maintainer choose | Maintainer decision recorded |
| 3 | Keep D-1 flagged as out-of-scope in every downstream project artifact until upstream closes it | D-1 referenced in `RISK_STATUS_REPORT.md` §3 |
| 4 | Re-check after the next submodule bump | `pytest harness/tests/test_file_size_ratchet.py` PASS |

### Owner and target

- **Owner**: harness-methodology maintainer (external). Escalation is routed by
  TECH_LEAD; this project owns only the reporting step.
- **Target**: next harness release / next submodule bump. **No date is committed
  by this project** — the deadline is outside our control and is recorded as
  such rather than invented.

### Residual risk

Unchanged (10) until upstream acts. This project's exposure is capped at "one
known-failing harness self-test"; it does not affect `taskq` correctness, and it
is explicitly non-blocking for Gate 4 (`failing_dimensions = []`).

---

## Cross-Plan Verification Checklist

Run after MP-01 and MP-02 land (MP-03 is externally owned):

```bash
.venv/bin/python -m pytest 03-development/tests -q          # 0 unexpected skips in NFR-07 / NFR-10
.venv/bin/python -m pytest --cov=03-development/src --cov-fail-under=100
.venv/bin/python harness_cli.py spec-coverage-check --project . --threshold 90.0
ruff check . && .venv/bin/python -m mypy . --ignore-missing-imports
```

Acceptance: NFR-07 and NFR-10 skips are gone, coverage stays at 100 %, D4
spec-coverage stays ≥ 90 %, lint/type clean.

---

## Self-Review

**Where these plans could be wrong**

1. **MP-01/MP-02 may be larger than scoped.** Both assume the production code is
   nearly correct and only the tests are missing. If `store`/`breaker`/`cache`
   turn out to lack the backup and fail-fast logic entirely, each plan becomes a
   feature implementation, not a test-writing exercise, and the P9-exit target
   is unrealistic.
2. **MP-03 may be mis-owned.** If the harness ratchet ceiling is configurable
   from project-side config rather than harness source, the escalation is
   unnecessary and the risk could be closed locally without violating HR-17.
   This was not verified — verifying it requires reading harness test internals.

**Unverified assumptions**

- Projected phase-exit dates (2026-07-30) reflect current pipeline velocity, not
  a human commitment.
- The skip messages in `test_nfr4_fault.py` accurately describe the state of the
  production code they guard.

**Confidence**: Medium. Plan structure and ownership are sound; effort estimates
and dates are the weakest element.
