# CONFIG_RECORDS.md - run-all-by-workflow

> On-demand Lazy Load template.

## 1. Version Information
- Version: vharness-v4-20260728-score98-10-g29c92b0
- Git Commit: 29c92b0
- Release Date: 2026-07-28

## 2. Runtime Configuration
| Environment | Config |
|-------------|--------|
| Development | {{config}} |
| Production | {{config}} |

## 3. Dependency List
```
{{pip freeze / npm lock output}}
```

## 4. Environment Variables
| Variable | Type | Description |
|----------|------|-------------|
| {{VAR}} | secret | {{description}} |

## 5. Deployment Log
| Date | Version | Method | Executor |
|------|---------|--------|----------|
| 2026-07-28 | harness-v4-20260728-score98-10-g29c92b0 | {{method}} | {{name}} |

## 6. Configuration Change Log
| Phase | Change | Rationale |
|-------|--------|----------|
| Phase 8 | {{change}} | {{reason}} |

## 7. Rollback SOP
**Trigger Condition**: {{condition}}
**Commands**:
```bash
{{rollback commands}}
```

## 8. Configuration Compliance
- [ ] Phase 7 risk mitigations implemented
- [ ] Monitoring thresholds configured
- [ ] Circuit breaker enabled

## Human Context (P8 append)

### Ownership per Config Item
| Config Item | Primary Owner | Backup Owner | Source-of-Truth Module |
|-------------|---------------|--------------|------------------------|
| Harness pipeline orchestration | Johnny (project lead) | Claude (P8 reviewer) | `harness/scripts/phase8_doc_gen.py` |
| Phase plans (`.methodology/phaseN_plan.md`) | Johnny | Claude | `.methodology/state.json` |
| `language` / `test_runner` in state.json | Johnny | Claude | `.methodology/state.json` |
| FR Registry (Gate 1) | Claude (during phase execution) | Johnny | `.methodology/state.json` |
| Git submodule `harness-methodology` | Johnny (init owner) | Claude | `harness/` submodule |
| 08-config artifacts (this file, RELEASE_CHECKLIST) | Claude (P8) | Johnny | `08-config/` |
| CI pipeline configuration | Johnny | Claude | `.github/workflows/` |
| `.venv` Python interpreter | Johnny | — | `.venv/bin/python` |

### Secret Rotation Cadence
| Secret Class | Rotation Frequency | Storage Location | Rotation Owner |
|--------------|--------------------|------------------|----------------|
| CI / GitHub Actions secrets | 90 days | GitHub Encrypted Secrets | Johnny |
| API keys referenced in env vars | 90 days | `.env` (gitignored) / secret manager | Johnny |
| Submodule deploy tokens (if any) | 180 days | GitHub PAT / SSH key | Johnny |
| Local dev `.venv` tokens (if any) | 180 days | OS keychain | Johnny |

> Cadence reflects industry baseline; tighten to 30/60 days if any secret is internet-facing.

### Access Audit Log Reference
- GitHub repository audit log: `Settings → Audit log` (Johnny has access; filtered to `run-all-by-workflow`).
- Local file changes on config artifacts: tracked via git history on `08-config/CONFIG_RECORDS.md` and `08-config/RELEASE_CHECKLIST.md`.
- Harness script invocations: log retained in `.methodology/state.json` `history` field (populated by `harness/ssi/prompts/evaluate_dimension.md` and phase scripts).
- Permission / role changes: reviewed monthly during Gate 4 quality scan (P6).

