# Risk Register — run-all-by-workflow (`taskq` v0.1.0)

> **Phase**: 7 — Risk Management
> **Date**: 2026-07-28
> **Baseline commit**: `7a691d1` (`harness-v4-20260728-score98-8-g7a691d1`)
> **Authoring role**: P7 Risk Author (Agent A / QA)
> **Framework**: harness-methodology v2.12.0

---

## 1. Scope and Sources

This register consolidates every open and closed risk known at the P7 entry
baseline. Nothing here is invented: each row cites the artifact it was derived
from.

| Source | Path | What was taken |
|--------|------|----------------|
| SPEC risk matrix §9 | `SPEC.md` | Seed risks R1–R9 (name, impact, likelihood, mitigation) |
| Gate 3 result | `.methodology/gate3_result.json` | Dimension scores, `open_critical=0`, `open_high=0` |
| Gate 4 result | `.methodology/gate4_result.json` | Dimension scores, `failing_dimensions=[]`, mutation-testing feature-flag exclusion |
| Bug-hunt report | `.methodology/bug_hunt_report.json` | 3 confirmed HIGH findings, all `resolution.status = resolved` at `e8805d0` |
| P5 baseline known issues | `05-verification/BASELINE.md` §5 | D-1 (MEDIUM), D-2 / D-3 (LOW) |
| Final sign-off | `FINAL_SIGN_OFF.md` §4 | NFR-09 DEFERRED; NFR-01..NFR-10 status table |
| Skipped-test inventory | `03-development/tests/test_nfr4_fault.py`, `test_nfr_scan.py` | 10 `pytest.skip` sites = coverage gaps behind NFR-07/08/09/10 and NFR-06 |
| Deferred fixes | `.methodology/deferred_fixes.md` | **Absent** — no file on disk at P7 entry; treated as "no additional deferrals recorded" |
| Issue registry | `.sessi-work/issue_registry.json` | **Absent** — no file on disk at P7 entry; Gate 3/4 JSON used as the substitute issue source |

> **Unverified assumption (flagged)**: because `deferred_fixes.md` and
> `issue_registry.json` do not exist in this repository, the open-issue set is
> reconstructed from Gate 3/4 JSON, the P5 baseline, and the skip inventory. If
> those two files are produced later, this register must be re-reconciled.

---

## 2. Scoring Model

- **Likelihood (L)**: 1 = rare, 2 = unlikely, 3 = possible, 4 = likely, 5 = occurring now.
- **Impact (I)**: 1 = cosmetic, 2 = degraded quality/ops, 3 = functional defect, 4 = severe defect, 5 = data loss / security breach.
- **Inherent score** = L×I **before** the mitigations shipped in P3–P6.
- **Current score** = L×I **as assessed at the P7 baseline**, i.e. after shipped mitigations.
- **Band** (applied to the *current* score): `≥ 9` HIGH · `4–8` MEDIUM · `≤ 3` LOW.
- Every risk with **current score ≥ 9** requires a formal plan in
  [`RISK_MITIGATION_PLANS.md`](RISK_MITIGATION_PLANS.md).

SPEC §9 uses 高/中/低 labels; they are mapped to the numeric scale as
高 = 5 (impact) / 4 (likelihood), 中 = 3, 低 = 2 (impact) / 1–2 (likelihood).

---

## 3. Risk Register

| ID | Name | Category | Inherent L×I | Current L | Current I | Current score | Band | Mitigation approach | Status |
|----|------|----------|--------------|-----------|-----------|---------------|------|---------------------|--------|
| R1 | 並發寫入損壞 `tasks.json` (concurrent write corruption) | reliability / concurrency | 3×5 = 15 | 1 | 5 | **5** | LOW | `fcntl.flock` exclusive lock + tmp-file `os.replace` atomic write (NFR-03/NFR-08); read-modify-write in `cli._cmd_submit` moved inside the lock | **Mitigated** — bug-hunt `taskq.cli#1` + `taskq.store#1` resolved at `e8805d0`; repro test `03-development/tests/integration/test_bug_hunt_repros.py` |
| R2 | subprocess 懸掛/殭屍 (hung or zombie child process) | reliability | 3×3 = 9 | 1 | 3 | **3** | LOW | Mandatory `timeout=` on every `subprocess.run` (FR-02); harness `preflight_reliability_lint` blocks any timeout-less call from P4 onward | **Mitigated** — FR-02 Gate 1 = 100.0, 10/10 tests PASS |
| R3 | breaker 誤鎖死 (circuit breaker latches OPEN) | reliability | 2×3 = 6 | 1 | 3 | **3** | LOW | `TASKQ_BREAKER_COOLDOWN` + HALF_OPEN probe transition (FR-03); NFR-03 asserts OPEN→CLOSED recovery ≤ cooldown + 1 s | **Mitigated** — FR-03 Gate 1 = 100.0, 7/7 tests PASS |
| R4 | 快取回放陳舊結果 (stale cache replay) | correctness | 3×2 = 6 | 1 | 2 | **2** | LOW | TTL expiry forces re-execution (FR-04); TTL read from `TASKQ_CACHE_TTL` via `config.py` | **Mitigated** — FR-04 Gate 1 = 100.0, 6/6 tests PASS |
| R5 | secret 落盤洩漏 (secret persisted to disk) | security | 3×5 = 15 | 1 | 5 | **5** | LOW | `_redact()` on `stdout_tail`/`stderr_tail` before persistence to `tasks.json`/`cache.json` (NFR-04) | **Mitigated** — bug-hunt `taskq.executor#1` resolved at `e8805d0`; gitleaks 69 commits, 0 leaks; `detect-secrets` 0 findings |
| R6 | fault injection 干擾正常執行 | reliability | 3×3 = 9 | 1 | 3 | **3** | LOW | Injection reachable only via explicit `--inject-fault=<scenario>` CLI flag or unit-test monkeypatch; production path never enables it (NFR-07) | **Mitigated** — bandit B105 finding on the flag sentinel triaged as false-positive |
| R7 | flock 在 NFS / 網路檔案系統失效 | reliability / portability | 3×3 = 9 | 2 | 3 | **6** | MEDIUM | Best-effort flock; on network FS degrade to "atomic write, no flock" + `WARNING` (NFR-08). Primary defence remains NFR-03 atomic write | **Partially mitigated** — degradation path implemented, but the exclusive-vs-shared lock contract is single-mode (exclusive only); reader fan-in test skipped at `test_nfr4_fault.py:236` |
| R8 | 1000 tasks 觸發 memory limit | scalability | 2×3 = 6 | 2 | 3 | **6** | MEDIUM | Streaming iterator; never materialise all tasks in memory (NFR-09, < 100 MB peak) | **Partially mitigated** — peak-memory fixture `test_nfr09_03_peak_memory_bound` PASS; the 100-task lossless `run --all` test is skipped at `test_nfr4_fault.py:275` |
| R9 | schema migration 失敗導致資料遺失 | data integrity / evolvability | 2×5 = 10 | 2 | 5 | **10** | **HIGH** | Backup `<file>.v<n>.bak` before migrate; on failure keep the backup and `exit 1` fail-fast; reject `version > 1` (NFR-10) | **Open — verification gap**: lazy v0→v1 upgrade is implemented in `store.load_store`, but both migration tests are skipped (`test_nfr4_fault.py:280`, `:287`), so the backup + fail-fast branch has no behavioural proof |
| R10 | NFR-09 1000-task p95 < 100 ms budget unenforced | performance | 4×2 = 8 | 4 | 2 | **8** | MEDIUM | Benchmark fixture `test_nfr09_01_1000_task_p95_benchmark` exists; enable it after a perf-optimisation pass | **Open — accepted deferral**: `FINAL_SIGN_OFF.md` §4 records NFR-09 as DEFERRED, non-blocking for Gate 4; skip at `test_nfr4_fault.py:259` |
| R11 | NFR-07 fault recovery unproven for `breaker.json` / `cache.json` | reliability | 3×3 = 9 | 3 | 3 | **9** | **HIGH** | Extend the fault-injection matrix from `tasks.json` to the other two data files; assert either auto-recovery or fail-fast, never silent rebuild | **Open — verification gap**: three skips at `test_nfr4_fault.py:189`, `:195`, `:200`; `tasks.json` path is proven, the other two are not |
| R12 | `.env.example` not shipped — NFR-06 config contract uninstantiated | deployability | 4×2 = 8 | 4 | 2 | **8** | MEDIUM | Ship `.env.example` declaring all 8 `TASKQ_*` variables with comments and defaults, matching `config.py` | **Open (D-2)**: `test_nfr_scan.py:98` skips conditionally because no `.env.example` exists at repo root (confirmed by directory listing) |
| R13 | harness file-size ratchet failing (`project_cmds.py` 2036 > ceiling 1986) | process / CI | 5×2 = 10 | 5 | 2 | **10** | **HIGH** | Harness-side decision required: split the file or raise the ratchet ceiling. HR-17 forbids this project from touching `harness/` | **Open (D-1)** — escalation, not a project defect; no `taskq` production code involved |
| R14 | Mutation testing disabled → unknown fault-detection strength | test quality | 3×2 = 6 | 3 | 2 | **6** | MEDIUM | `mutation_testing=false` in `.methodology/harness_config.json`; composite is renormalised without it. Compensating controls: 100 % line coverage, assertion-quality 92.8 | **Accepted (feature flag)** — re-enable if the suite is ever suspected of passing on weak assertions |
| R15 | Integration coverage margin is thin (77.0 vs threshold 75.0) | test quality | 3×2 = 6 | 3 | 2 | **6** | MEDIUM | Any new uncovered integration path can push the dimension below threshold and fail a future gate; add integration tests alongside new code | **Monitoring** — Gate 4 `integration_coverage = 77.0`, headroom = 2.0 points |
| R16 | `store.py:63` atomic-write `finally` branch uncovered | test quality | 2×2 = 4 | 1 | 2 | **2** | LOW | Fault-injection test that makes `os.replace` raise, forcing the `finally: tmp.unlink()` branch | **Closed (D-3)** — Gate 4 `test_coverage = 100.0` (400 stmts, 0 miss); the gap observed at P5 (99 %, 1 miss) no longer exists |

---

## 4. Band Summary

| Band | Count | Risk IDs |
|------|-------|----------|
| HIGH (current ≥ 9) | 3 | R9, R11, R13 |
| MEDIUM (4–8) | 6 | R7, R8, R10, R12, R14, R15 |
| LOW (≤ 3) | 7 | R1, R2, R3, R4, R5, R6, R16 |
| **Total** | **16** | — |

| Status | Count | Risk IDs |
|--------|-------|----------|
| Mitigated | 6 | R1, R2, R3, R4, R5, R6 |
| Partially mitigated | 2 | R7, R8 |
| Open | 5 | R9, R10, R11, R12, R13 |
| Accepted / monitoring | 2 | R14, R15 |
| Closed | 1 | R16 |

Note the asymmetry: the three HIGH risks are **verification gaps and one process
escalation**, not known functional defects. Gate 3 and Gate 4 both report
`open_critical_count = 0` and `open_high_count = 0` for *defects*; R9/R11 are
HIGH because the mitigation exists in code but is **not proven by an executing
test**, and R13 is HIGH because it is already failing and cannot be fixed from
inside this project.

---

## 5. Category Distribution

| Category | Risk IDs |
|----------|----------|
| reliability / concurrency | R1, R2, R3, R6, R7, R11 |
| security | R5 |
| correctness | R4 |
| data integrity / evolvability | R9 |
| performance / scalability | R8, R10 |
| deployability | R12 |
| test quality | R14, R15, R16 |
| process / CI | R13 |

---

## 6. Traceability to Requirements

| Risk | Requirement anchor | Implementing module |
|------|--------------------|---------------------|
| R1, R7 | NFR-03, NFR-08 | `taskq.store` |
| R2, R6 | FR-02, NFR-07 | `taskq.executor` |
| R3 | FR-03, NFR-03 | `taskq.breaker` |
| R4 | FR-04 | `taskq.cache` |
| R5 | NFR-04 | `taskq.executor` |
| R8, R10 | NFR-09 | `taskq.store` |
| R9 | NFR-10 | `taskq.store` |
| R11 | NFR-07 | `taskq.store`, `taskq.breaker`, `taskq.cache` |
| R12 | NFR-06 | `taskq.config` |
| R13 | — (harness process) | `harness/cli/project_cmds.py` |
| R14, R15, R16 | quality dimensions | test suite |

---

## 7. Self-Review

**Where this register could be wrong**

1. **Skip-to-risk inference may over- or under-state R9/R11.** The register
   treats each `pytest.skip` as an unproven mitigation. If the skipped
   behaviour is in fact exercised indirectly by an integration test not
   inspected here, the current likelihood for R9/R11 is overstated. Conversely,
   if the lazy v0→v1 path never writes a `.v<n>.bak` at all, R9's impact is
   understated.
2. **SPEC 高/中/低 → numeric mapping is a judgement call.** A different mapping
   (e.g. 中 = 4) would push R7 and R8 into the HIGH band and require formal
   plans for them. The mapping used is stated explicitly in §2 so the reader can
   re-derive.

**Unverified assumptions**

- `deferred_fixes.md` and `issue_registry.json` are absent, not empty-by-error.
- No risk was introduced between commit `e8805d0` (bug-hunt fixes) and `7a691d1`
  (P7 per-FR Gate 1 re-runs); the per-FR Gate 1 results at phase 7 are all
  score 100.0, which supports but does not prove this.
- Target dates in [`RISK_MITIGATION_PLANS.md`](RISK_MITIGATION_PLANS.md) are
  anchored to phase exits, not to a calendar commitment from a human owner.

**Confidence**: Medium-High. The risk *inventory* is high confidence (every row
traces to a file on disk). The *scores* are medium confidence because the
likelihood values are analyst judgement, not measured frequencies.
