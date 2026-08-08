# Security Policy

## Supported versions

The project is pre-1.0. Security fixes are applied to the latest `0.1.x` release candidate until a formal support policy is published.

## Reporting a vulnerability

Do not open a public issue for vulnerabilities, exposed credentials, account identifiers or production evidence. Use the repository's private GitHub Security Advisory form. If that form is unavailable, email `gpt@tiktok111.com` with the subject `wechat-official-account-pipeline security report`.

Include only the minimum reproduction needed. Redact secrets from the report and attachments, and do not test against an account, follower list or provider that you do not own.

## Credential exposure

If a real AppSecret, API key, webhook token or private key is exposed:

1. Revoke or rotate it at the provider first.
2. Stop affected automation and inspect publish intents and receipts.
3. Remove it from the working tree and prepare a clean history for any public repository.
4. Do not assume deleting the latest file removes it from Git history.

## Security boundaries

- `.env` and runtime artifacts are ignored and must remain local.
- The public default denies providers and WeChat writes.
- Publishing requires an authorized runtime identity, a frozen package and idempotency state.
- Logs redact common credential fields but should still be treated as private operational data.
- The project cannot make third-party feeds, generated content or platform policy compliant on the operator's behalf.
