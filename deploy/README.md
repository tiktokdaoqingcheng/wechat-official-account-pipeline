# Deployment templates

These files are reviewable examples, not an installer that should be run unchanged.

- The example base directory is `/srv/wechat-official-account-automation`.
- Timer values are illustrative local times and must be reviewed with the schedule configuration.
- The configuration page listens on `127.0.0.1` only.
- Public defaults keep WeChat writes and external providers disabled and use `dry_run`.
- Copy credentials into an untracked environment file only after completing the security and permission checklists.

Before enabling any timer, inspect every unit, create a private schedule override, run the synthetic demo and a no-write permission probe, and confirm that only one runtime instance owns the publishing lease.
