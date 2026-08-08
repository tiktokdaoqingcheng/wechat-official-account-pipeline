# Security model

## Assets to protect

- WeChat AppSecret, access tokens and callback secrets.
- Model and image-provider credentials.
- Webhook tokens and private endpoints.
- Unpublished drafts, source corpora and follower-related data.
- Publish intents, media identifiers, message identifiers and operational logs.

## Main threats

| Threat | Control |
| --- | --- |
| Secret committed to Git | Ignored `.env`, empty example values, public-tree scan and external secret scan before release |
| Duplicate publish after timeout | Intent-before-submit, stable client identifier, status reconciliation and daily lock |
| Wrong host publishes | Runtime identity allowlist and explicit write capability |
| Model invents or rewrites facts | Source fact cards, field validation, deterministic final checks and human hold path |
| Weak item removal leaves an empty article | Hard minimums and reserve-item replenishment |
| Provider outage causes cost amplification | Per-role retry cap, circuit opening, call ledger and daily budget |
| Changed artifact is published | Frozen manifest and hash verification |
| Log leaks credentials | Sensitive-field redaction and private runtime storage |
| Unlicensed content enters release | Synthetic public fixtures and documented asset provenance |

## Defaults

`config/schedule.yaml` sets `allow_wechat_writes: false`, `allow_external_providers: false` and `publishing.mode: dry_run`. `.env.example` contains no value. Public news feeds are synthetic and disabled.

## Human-in-the-loop boundary

Human review is not a cosmetic approval button. `manual_confirm` generates a request tied to the reviewed package. A later content or configuration change invalidates the assumptions behind that confirmation and requires a new package.

## Operational guidance

- Use a dedicated, least-privilege service account and stable egress.
- Restrict `.env`, state, logs and backups at the filesystem level.
- Back up configuration separately from immutable releases.
- Rotate any credential ever pasted into chat, a ticket or a shared log.
- Do not run prepare and publish controllers concurrently.
- Treat an unresolved publish intent as a reconciliation incident, not permission to retry.
