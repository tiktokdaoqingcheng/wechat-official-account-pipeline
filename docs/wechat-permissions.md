# WeChat permissions and preflight

Last verified against official documentation: **2026-08-08**.

Platform policy changes independently of this project. The account's live **接口权限** page is authoritative.

## Capability map

| Project mode or step | WeChat capability | Official path |
| --- | --- | --- |
| Access token | Server API credentials and any required outbound-IP allowlist | Server API guide |
| Article images | Upload article image / permanent material | Material management |
| Draft creation | Add draft | `/cgi-bin/draft/add` |
| Free publish | Submit draft for publication | `/cgi-bin/freepublish/submit` |
| Mass send all | Advanced mass send by filter | `/cgi-bin/message/mass/sendall` with `is_to_all: true` |
| Reconciliation | Query free-publish or mass-send status | Corresponding status API |

Official references:

- [Draft management](https://developers.weixin.qq.com/doc/offiaccount/Draft_Box/Add_draft.html)
- [Publishing](https://developers.weixin.qq.com/doc/offiaccount/Publish/Publish.html)
- [Advanced mass send](https://developers.weixin.qq.com/doc/service/guide/product/message/Batch_Sends.html)
- [Server API guide](https://developers.weixin.qq.com/doc/service/guide/dev/api/)

## Account checks

Before any real test:

1. Confirm the account type and verification status.
2. Confirm AppID and AppSecret are held outside Git.
3. Confirm the server's current outbound IP is allowed when the platform requires it.
4. Confirm draft, media and chosen publish-channel permissions in the dashboard.
5. Confirm daily or monthly quota is available.
6. Confirm whether API mass-send protection requires an administrator action.
7. Confirm notification and callback handling for asynchronous results.

The official publishing page states that, from July 2025, personal accounts, unverified enterprise accounts and accounts that cannot be verified lose access to the listed publishing APIs.

The official mass-send guide describes different quotas for verified official accounts and verified service accounts. It also states that an account with API mass-send protection enabled may require administrator confirmation; rejection or no confirmation within thirty minutes fails the send. These are platform controls and must not be bypassed.

## Originality and duplicate protection

Mass-send content is subject to the platform's originality checks. The project exposes `send_ignore_reprint` but defaults it to false. Enable it only after understanding the official reprint behaviour and holding the necessary rights.

The project generates a stable, bounded `clientmsgid` for mass-send requests. This complements local intents and locks but does not replace status reconciliation.

## Safe rollout

Use the following progression:

1. Offline synthetic `dry_run`.
2. Real sources with external providers still disabled.
3. Real providers in `dry_run` with a strict call budget.
4. `draft_only` on a test or approved account.
5. `manual_confirm` with an operator present.
6. Guarded auto-publish only after unattended-run evidence and alerting are reliable.
