# Open-source audit summary

Audit date: **2026-08-08**

## Decision

The original private repository must not be made public and its history must not be reused for the public project. A separate public repository was created from the reviewed, sanitised candidate without importing the private production history:

`https://github.com/tiktokdaoqingcheng/wechat-official-account-pipeline`

No verified production credential was found in tracked Git history. However, the private history contains operational metadata that is inappropriate for publication, including production paths, host information, publish receipts, message identifiers, real article URLs and detailed incident/deployment records. Ignored runtime directories also contain credentials, generated articles, logs and databases.

## Scope and methods

- Current tree and ignored-file inventory.
- Full Git object/history text scan.
- Gitleaks scan of history and working tree.
- Public IP, workstation path, identifier and credential-pattern checks.
- Binary and archive inventory.
- Dependency licence metadata review.
- Manual review of tracked images, article fixtures and source configuration.

## Excluded from the clean candidate

- Private state memory, secrets guidance and deployment records.
- `.env`, virtual environments, databases, logs, outputs, publish state and backups.
- Historical release archives and generated article/image corpora.
- Real RSS source presets and private provider defaults.
- Existing cover images without auditable provenance.
- Tests containing real bylines, advertising copy or article-derived examples.

## Included after remediation

- Application source, focused tests and generic deployment templates.
- Empty `.env.example` and non-publishing defaults.
- Disabled synthetic source configuration.
- Fictional demonstration data using `.invalid` domains.
- Original deterministic cover assets and provenance records.
- Apache-2.0 licence draft, bilingual documentation, contribution/security policies and CI.

## Copyright and provenance review

- No third-party source tree, font, stock photograph, article corpus or generated production draft is bundled.
- Seven cover PNGs are deterministic geometric output from `scripts/generate_demo_assets.py`; their inputs and regeneration command are documented.
- The documentation screenshot is rendered solely from the fictional records under `examples/synthetic/`.
- Content and incident fixtures use fictional organisations, fictional publications and reserved example domains.
- Remaining third-party names and domains are limited to API interoperability, dependency licence links, RSS namespaces and trademark notices.

## Local validation

The candidate was validated on 2026-08-08 in a fresh Python 3.12 virtual environment using the locked runtime and development dependencies:

- Public-tree safety checker: passed.
- Python compilation: passed.
- Ruff: passed.
- Unit tests: 316 passed.
- Fixed-date synthetic demo: passed with `dry_run`, `published=false`, low risk, six primary items and four secondary items.
- Frozen artifact manifest: verified.
- Demo screenshot: manually reviewed for synthetic-only content.

The public repository then passed the complete GitHub Actions matrix on Python 3.11 and 3.12. Both jobs ran the public-tree checker, compilation, Ruff, all 316 tests and the fixed-date synthetic demo.

## Owner decisions completed

- Apache-2.0 and the copyright holder `daoqingcheng` have been explicitly confirmed.
- The public repository is `tiktokdaoqingcheng/wechat-official-account-pipeline`.
- The private security contact is `gpt@tiktok111.com`.
- Public repository creation and v0.1.0 publication are approved after successful release checks.

## Remaining repository controls

- Apply the documented repository topics and full description in GitHub settings.
- Enable private vulnerability reporting and GitHub Security Advisories.
- Protect `main`, require pull requests and require the CI check when the repository plan and owner settings permit it.
- Keep production secrets, real content, deployment evidence and private incident history outside this repository.
