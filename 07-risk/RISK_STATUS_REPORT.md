# Risk Status Report — run-all-by-workflow (`taskq` v0.1.0)

> **Phase**: 7 — Risk Management
> **Reporting date**: 2026-07-28
> **Baseline commit**: `7a691d1` (`harness-v4-20260728-score98-8-g7a691d1`)
> **Companion documents**: [`RISK_REGISTER.md`](RISK_REGISTER.md) · [`RISK_MITIGATION_PLANS.md`](RISK_MITIGATION_PLANS.md)

---

## 1. Executive Summary

16 risks are tracked. **3 are HIGH** (current score ≥ 9), 6 MEDIUM, 7 LOW.

The three HIGH risks are **not open defects**. Gate 3 and Gate 4 both report
`open_critical_count = 0` and `open_high_count = 0`, and `failing_dimensions` is
empty at Gate 4 (composite **97.4 / 100**, PASS). The HIGH band is occupied by:

- two **verification gaps** (R9, R11) — mitigation code exists but is guarded by
  `pytest.skip`, so its behaviour is unproven;
- one **process escalation** (R13) — a harness self-test that this project is
  forbidden by HR-17 to fix.

All three confirmed HIGH findings from the P4 adversarial bug hunt
(`taskq.cli#1`, `taskq.store#1`, `taskq.executor#1`) are **resolved** at commit
`e8805d0` with repro tests in
`03-development/tests/integration/test_bug_hunt_repros.py`. Those correspond to
register rows R1, R5 and part of R7/R8's mitigation chain, and are now LOW.

**Release posture**: no risk in this register blocks the v0.1.0 sign-off. R9,
R11 and R12 should be closed during P9 maintenance before the next release.

---

## 2. Status Dashboard

| Metric | Value | Source |
|--------|-------|--------|
| Risks tracked | 16 | `RISK_REGISTER.md` §3 |
| HIGH (≥ 9) | 3 (R9, R11, R13) | §3 below |
| MEDIUM (4–8) | 6 (R7, R8, R10, R12, R14, R15) | §4 below |
| LOW (≤ 3) | 7 (R1–R6, R16) | §5 below |
| Formal mitigation plans | 3 (MP-01, MP-02, MP-03) | `RISK_MITIGATION_PLANS.md` |
| Open critical defects | 0 | `.methodology/gate4_result.json` |
| Open high defects | 0 | `.methodology/gate4_result.json` |
| Gate 4 composite | 97.4 / 100 PASS | `.methodology/quality_manifest.json` |
| Failing quality dimensions | `[]` | `.methodology/gate4_result.json` |
| Bug-hunt findings confirmed / resolved | 3 / 3 | `.methodology/bug_hunt_report.json` |
| Unexpected test skips behind NFRs | 9 (`test_nfr4_fault.py`) + 1 (`test_nfr_scan.py`) | skip inventory |
| Release-blocking risks | **0** | this report |

---

## 3. HIGH Risks — Detail

| Risk | Name | Score | Status | Mitigation owner | Plan | Target date |
|------|------|-------|--------|------------------|------|-------------|
| **R9** | Schema migration failure → data loss (NFR-10) | 10 | **Open — verification gap.** Lazy v0→v1 upgrade implemented in `store.load_store`; `.v<n>.bak` backup + fail-fast branch has no executing test (skips at `test_nfr4_fault.py:280`, `:287`) | Agent A — Development (P9 maintenance TDD chain) | [MP-01](RISK_MITIGATION_PLANS.md#mp-01--prove-the-nfr-10-schema-migration-safety-net-r9) | P9 exit — projected **2026-07-30** |
| **R11** | NFR-07 fault recovery unproven for `breaker.json` / `cache.json` | 9 | **Open — verification gap.** `tasks.json` fault path proven; other two files skipped at `test_nfr4_fault.py:189`, `:195`, `:200` | Agent A — Development (P9 maintenance TDD chain) | [MP-02](RISK_MITIGATION_PLANS.md#mp-02--extend-nfr-07-fault-injection-coverage-to-all-three-data-files-r11) | P9 exit — projected **2026-07-30** |
| **R13** | harness file-size ratchet failing (`project_cmds.py` 2036 > 1986) | 10 | **Open — escalated.** D-1 from `05-verification/BASELINE.md` §5. HR-17 forbids modifying `harness/`; no `taskq` code involved | harness-methodology maintainer (external; routed by TECH_LEAD) | [MP-03](RISK_MITIGATION_PLANS.md#mp-03--escalate-the-harness-file-size-ratchet-failure-r13) | Next harness release — **no date committed by this project** |

---

## 4. MEDIUM Risks — Watch List

No formal plan required (score < 9). Each has a named owner and a review trigger.

| Risk | Name | Score | Status | Mitigation owner | Target / trigger |
|------|------|-------|--------|------------------|------------------|
| R7 | flock ineffective on NFS / network FS | 6 | Partially mitigated — degradation to "atomic write + WARNING" implemented; shared-vs-exclusive lock contract is exclusive-only, reader fan-in test skipped (`test_nfr4_fault.py:236`) | Agent A — Development | Review at P9 entry; promote to HIGH if `$TASKQ_HOME` on a network mount becomes a supported configuration |
| R8 | 1000-task memory ceiling (NFR-09) | 6 | Partially mitigated — peak-memory fixture PASS; 100-task lossless `run --all` skipped (`:275`) | Agent A — Development | Close together with R10 in the perf pass |
| R10 | NFR-09 1000-task p95 < 100 ms unenforced | 8 | Open — accepted deferral, recorded in `FINAL_SIGN_OFF.md` §4 as non-blocking for Gate 4 | Agent A — Development | Next perf-optimisation pass (post-v0.1.0); re-enable `test_nfr09_01_1000_task_p95_benchmark` |
| R12 | `.env.example` not shipped (NFR-06 contract uninstantiated, D-2) | 8 | Open — `test_nfr_scan.py:98` skips conditionally; no `.env.example` at repo root | Agent A — P8 Config Management author | **P8 exit** — natural fit with `08-config/CONFIG_RECORDS.md` |
| R14 | Mutation testing disabled by feature flag | 6 | Accepted — `mutation_testing=false`; composite renormalised. Compensating controls: 100 % line coverage, assertion quality 92.8 | TECH_LEAD | Re-evaluate if any post-release defect escapes the suite |
| R15 | Integration coverage margin 77.0 vs threshold 75.0 | 6 | Monitoring — headroom 2.0 points | Agent A — Development | Check at every gate; add integration tests with any new code path |

> R12 is the one MEDIUM risk with a near-term, cheap close: shipping
> `.env.example` with the 8 `TASKQ_*` variables removes a conditional skip and
> instantiates the NFR-06 deployability contract during P8.

---

## 5. LOW Risks — Mitigated / Closed

| Risk | Name | Score | Status | Evidence |
|------|------|-------|--------|----------|
| R1 | Concurrent write corruption of `tasks.json` | 5 | Mitigated | flock + atomic write; bug-hunt `taskq.cli#1` + `taskq.store#1` resolved at `e8805d0`; post-fix probe 1/10 accepted (expected 1) |
| R2 | subprocess hang / zombie | 3 | Mitigated | Mandatory `timeout=`; `preflight_reliability_lint` blocks regressions; FR-02 Gate 1 = 100.0 |
| R3 | Breaker latches OPEN | 3 | Mitigated | cooldown + HALF_OPEN; FR-03 Gate 1 = 100.0 |
| R4 | Stale cache replay | 2 | Mitigated | TTL expiry re-execution; FR-04 Gate 1 = 100.0 |
| R5 | Secret persisted to disk | 5 | Mitigated | `_redact()` before persistence; bug-hunt `taskq.executor#1` resolved; gitleaks 0 leaks / detect-secrets 0 findings |
| R6 | Fault injection reaching production path | 3 | Mitigated | Explicit `--inject-fault` flag or monkeypatch only; bandit B105 triaged false-positive |
| R16 | `store.py:63` uncovered `finally` branch (D-3) | 2 | **Closed** | Gate 4 `test_coverage = 100.0` (400 stmts, 0 miss) — the P5-era 1-statement miss is gone |

---

## 6. Trend — Gate 3 → Gate 4

| Signal | Gate 3 (P4) | Gate 4 (P6) | Direction |
|--------|-------------|-------------|-----------|
| Composite score | 97.14 | 97.40 | ↑ +0.26 |
| Line coverage | 99 % (5 miss) | 100 % (0 miss) | ↑ closes R16 |
| Integration coverage | 74.0 (thr 60) | 77.0 (thr 75) | ↑ score, ↓ headroom (R15) |
| Traceability | n/a at G3 weighting | 94.3 (thr 90) | pass |
| Open critical / high defects | 0 / 0 | 0 / 0 | flat |
| Known issues | D-1, D-2, D-3 | D-1, D-2 (D-3 closed) | ↓ |

Risk direction is improving on defects and coverage; the residual exposure has
shifted from "unknown defects" to "unproven mitigations" (R9, R11) and
"deferred non-functional budgets" (R8, R10).

---

## 7. Recommended Actions, Ordered

1. **P8**: ship `.env.example` → closes R12 and one conditional skip.
2. **P9**: execute MP-01 (R9) then MP-02 (R11) as TDD chains → removes both HIGH
   verification gaps; expected residual 5 and 3.
3. **P9 entry**: re-assess R7 once the shared/exclusive lock contract is decided.
4. **Post-v0.1.0**: perf pass → closes R10 and the remainder of R8.
5. **Continuous**: report R13 upstream; do not attempt a local fix (HR-17).

---

## 8. Self-Review

**Where this report could be wrong**

1. **The "0 open high defects" headline could mislead.** Gate 3/4 counts refer to
   *defects found by the gate*, not to unproven mitigations. If a reader treats
   R9/R11 as already-safe because the gate is green, the report has failed its
   purpose — hence the explicit split in §1 between defects and verification
   gaps.
2. **The Gate 3 → Gate 4 trend mixes weightings.** The two gates use different
   dimension weights and thresholds (e.g. integration coverage threshold moves
   60 → 75), so the +0.26 composite delta is not a like-for-like measurement of
   improvement, only a directional signal.

**Unverified assumptions**

- `deferred_fixes.md` and `.sessi-work/issue_registry.json` do not exist in this
  repository; the open-issue set was reconstructed from Gate 3/4 JSON, the P5
  baseline, and the `pytest.skip` inventory. If either file is produced later,
  §3–§5 must be re-reconciled.
- Projected target date 2026-07-30 reflects pipeline velocity, not a human
  commitment; R13's date is outside this project's control and is left uncommitted.

**Confidence**: Medium-High for the inventory and status (each row cites a file
on disk); Medium for the scores and dates (analyst judgement).

---

## 9. Provenance

| Artifact | Path |
|----------|------|
| Risk register | [`07-risk/RISK_REGISTER.md`](RISK_REGISTER.md) |
| Mitigation plans | [`07-risk/RISK_MITIGATION_PLANS.md`](RISK_MITIGATION_PLANS.md) |
| Gate 3 result | `.methodology/gate3_result.json` |
| Gate 4 result | `.methodology/gate4_result.json` |
| Quality manifest | `.methodology/quality_manifest.json` |
| Bug-hunt report | `.methodology/bug_hunt_report.json` |
| P5 baseline (D-1/D-2/D-3) | `05-verification/BASELINE.md` §5 |
| Final sign-off | `FINAL_SIGN_OFF.md` |
| SPEC risk matrix | `SPEC.md` §9 |
