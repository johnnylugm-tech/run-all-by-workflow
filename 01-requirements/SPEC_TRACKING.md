# Specification Tracking Matrix — taskq

> Human-readable tracking view for the functional requirements transcribed in `SRS.md` from the canonical root specification, `SPEC.md` v4.0.0. The Status column is machine-refreshed by `build_traceability` during `advance-phase`; status and score authority remains `quality_manifest.json`.

## Project Info

- Project Name: taskq
- Specification Version: v4.0.0
- Created: 2026-07-28
- Canonical Source: `SPEC.md`
- Requirements Baseline: `SRS.md`

## Specification Status

| FR ID | Spec Description | Intent Class | Decision Framework | Status | Notes |
|-------|------------------|--------------|--------------------|--------|-------|
| FR-01 | Submit and validate tasks, reject invalid or duplicate-name input without storage writes, and atomically persist accepted pending tasks. | Functional / validation and persistence | Validate acceptance criteria in `SRS.md`; detail design in `SAD.md` and tests in `TEST_SPEC.md`. | DRAFT | Owner: Agent A (requirements); source: `SPEC.md` §3, FR-01. |
| FR-02 | Execute one or all pending tasks with safe argument splitting, timeout handling, lifecycle transitions, result capture, bounded concurrency, and thread-safe storage. | Functional / execution and concurrency | Validate acceptance criteria in `SRS.md`; detail design in `SAD.md` and tests in `TEST_SPEC.md`. | DRAFT | Owner: Agent A (requirements); source: `SPEC.md` §3, FR-02. |
| FR-03 | Retry failed or timed-out tasks with exponential backoff and enforce a persistent global CLOSED/OPEN/HALF_OPEN circuit breaker. | Functional / resilience and state management | Validate acceptance criteria in `SRS.md`; detail design in `SAD.md` and tests in `TEST_SPEC.md`. | DRAFT | Owner: Agent A (requirements); source: `SPEC.md` §3, FR-03. |
| FR-04 | Replay valid SHA-256-keyed TTL cache results without subprocess execution and atomically persist successful results under concurrent access. | Functional / caching and concurrency | Validate acceptance criteria in `SRS.md`; detail design in `SAD.md` and tests in `TEST_SPEC.md`. | DRAFT | Owner: Agent A (requirements); source: `SPEC.md` §3, FR-04. |
| FR-05 | Provide the specified argparse CLI commands, global JSON output, task queries and clearing, and canonical exit-code behavior. | Functional / interface integration | Validate acceptance criteria in `SRS.md`; detail design in `SAD.md` and tests in `TEST_SPEC.md`. | DRAFT | Owner: Agent A (requirements); source: `SPEC.md` §§3 and 7, FR-05. |

## Completeness Check

- Functional requirements in `SRS.md`: 5 (`FR-01` through `FR-05`).
- Functional requirements represented above: 5 (`FR-01` through `FR-05`).
- Missing or duplicate FR IDs: none.
- Every FR has an assigned requirements owner, initial status, canonical `SPEC.md` citation, and downstream architecture/test references.

## Update Log

| Date | Change | By |
|------|--------|----|
| 2026-07-28 | Replaced the lazy-load placeholder with the complete five-FR tracking matrix. | Agent A |
