# Codex for Open Source application draft

This is a factual draft for owner review before submission. It intentionally contains no star count, user count, contributor count or adoption claim.

## Project

**wechat-official-account-pipeline**

An auditable, human-in-the-loop automation pipeline for WeChat Official Accounts, covering topic discovery, drafting, review, scheduling, publishing, and performance feedback.

## Why I am a core maintainer

I am the project's primary owner and maintainer. I defined the editorial workflow, safety boundaries and production operating model; reviewed failures from unattended runs; prioritised fixes across source discovery, drafting, content review, provider reliability and WeChat publishing; and authorised deployments and real publishing actions. For the open-source transition, I am responsible for provenance, security review, release policy and future contributor decisions.

Applicant details:

- Applicant name: `daoqingcheng`
- GitHub account and public namespace: `tiktokdaoqingcheng`
- Security contact: `gpt@tiktok111.com`

## Ecosystem problem

Most examples of Official Account automation stop at generating text or calling a publishing endpoint. Real unattended operation also needs source quality, content provenance, review evidence, provider cost controls, state recovery, idempotency, operator takeover and platform-specific permission handling. Without those controls, automation either publishes weak content or blocks unpredictably, and a naive retry can create cost or duplicate-send risk.

This project provides a reference implementation for treating content automation as an auditable workflow rather than a single prompt. It is especially useful to small editorial and developer teams that need a safe path from local dry-run to human-reviewed production.

## Capabilities completed today

- Topic selection and configurable feed discovery.
- Candidate ranking, event deduplication, promotional filtering and reserve pools.
- Two-article daily packages with hard minimums and replenishment.
- Source-grounded fact cards and role-based optional model integration.
- Deterministic copy, provenance and platform-risk review.
- Local covers and optional image-generation integration.
- Dry-run, draft-only, manual-confirmation and guarded auto-publish modes.
- WeChat draft, free-publish and mass-send workflows.
- Runtime identity, budgets, checkpoints, leases, frozen manifests, publish intents and locks.
- Notifications, watchdogs, local run history and incident replay.
- Synthetic offline demo, tests and CI for a safe public baseline.

Audience analytics ingestion is a future plan, not a completed capability. No public adoption metrics are claimed.

## How Codex is used

Codex is used as an engineering collaborator across the maintenance lifecycle:

1. Recover project context from structured state and deployment records before changing code.
2. Trace failures across runtime state, logs, checkpoints and provider responses.
3. Implement focused changes while preserving unrelated local work.
4. Add regression tests for content, state-machine and idempotency failures.
5. Run full test suites, dry-runs, secret scans and release checks.
6. Review documentation, provenance, security boundaries and release notes.
7. Prepare deployments and handoff records while keeping real credentials outside Git.

The open-source candidate itself was prepared with Codex-assisted history auditing, fixture sanitisation, documentation, CI and reproducibility checks. The maintainer remains responsible for review and every external side effect.

## Six-month maintenance plan

### Months 1-2: public baseline and contributor safety

- Publish v0.1.0 after owner approval and independent secret review.
- Triage installation and documentation issues.
- Add schema documentation and safer configuration diagnostics.
- Establish a reproducible security-reporting and release process.

### Months 3-4: adapters and reliability

- Define a stable source-adapter interface.
- Add permission probes that never publish or incur model cost.
- Improve Linux deployment examples and configuration migration.
- Expand incident fixtures for timeouts, partial provider failure and pending publishes.

### Months 5-6: evaluation and feedback

- Add opt-in audience-analytics adapters where officially supported.
- Publish synthetic editorial-quality evaluation sets.
- Add provider compatibility recipes maintained as documentation, not hidden defaults.
- Review the roadmap with issue and contribution evidence available at that time.

## Sustainability and governance

The project will keep non-publishing defaults, synthetic public fixtures and mandatory provenance for contributed assets. Changes to write permissions, retry behaviour or review gates require tests and explicit maintainer review. The project will not report fabricated adoption metrics; future applications and release notes will use only verifiable repository and operator evidence.
