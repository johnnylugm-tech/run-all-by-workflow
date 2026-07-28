# RELEASE_CHECKLIST

## Pre-Release Checks
- [ ] All P1-P7 phases completed and artifacts generated.
- [ ] CI pipeline fully passed.
- [ ] Final Sign Off approved.
- [ ] Production environment provisioned.
- [ ] Rollback plan documented.

## Human Context (P8 append)

### Deployment Runbook URL
- Internal: `harness/docs/RUNBOOK.md` (relative to repo root: `/Users/johnny/projects/run-all-by-workflow/harness/docs/RUNBOOK.md`).
- External (if published): TBD — Johnny to publish to team wiki before Gate 4 sign-off.

### Rollback Owner + On-Call
| Role | Primary | Backup |
|------|---------|--------|
| Rollback owner | Johnny | Claude (P8 reviewer) |
| On-call engineer | Johnny (current quarter) | Designated team rotation (TBD) |
| Approver for production revert | Johnny | — |

Rollback trigger: any P8 release that fails Gate 4 composite score ≥ 85, fails smoke test post-deploy, or trips a P7 risk-mitigation circuit breaker.

### Post-Release Monitoring Dashboard
- Local CLI: `harness/scripts/phase_gate_report.py --dashboard` (TBD link if served).
- Key panels to watch for the first 24h after release:
  - Phase gate composite score trend (target ≥ 85).
  - FR coverage matrix delta vs. previous release.
  - CI pipeline success rate (target ≥ 95% over 24h).
  - Hook / subroutine error rate in `harness/ssi/prompts/`.

### Customer Comms Template
> Subject: `[run-all-by-workflow] Release v{{version}} — {{YYYY-MM-DD}}`
>
> Hi team,
>
> We're shipping release `{{version}}` (git `{{short_hash}}`) on `{{date}}`.
>
> What changed:
> - {{bullet 1 — FR / phase driver}}
> - {{bullet 2 — quality metric delta}}
> - {{bullet 3 — known caveat if any}}
>
> Risk window: {{start_ts}} → {{end_ts}} ({{tz}}).
> Rollback owner: Johnny. On-call: {{oncall}}.
> Runbook: {{runbook_url}}.
> Dashboard: {{dashboard_url}}.
>
> Reply on this thread or page on-call if you observe regressions.
>
> — Johnny (project lead)

