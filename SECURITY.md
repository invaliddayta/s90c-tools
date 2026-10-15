# Risks

Use only a TV you own or are authorized to administer.
Only the build in the README has been tested.

- The root path depends on Developer Mode and vulnerable firmware helpers.
  Do not update, downgrade, flash, reset Smart Hub or change security settings
  as part of this procedure. If an upgrade is pending, stop.
- SDB is unencrypted and the TV may listen on IPv6 as well as IPv4.
  Use a trusted LAN. Never expose port 26101 to the internet.
- App removal can delete settings and login data. Database backups are not
  reinstallable app archives. Recovery depends on store availability.
- Turning auto-update off also stops automatic maintenance of retained apps.
  It does not guarantee that upgrade offers stop appearing.
- Guard changes refresh app metadata and can affect store/sync behavior.
  Apply only when nobody is watching.
- A failed request may already have run. Do not blindly repeat a write,
  uninstall or reboot after losing the result. Inspect the recorded state first.
- Root does not bypass every SMACK/UEP restriction. Do not replace global
  account/key files or invent boot hooks to work around a failure.

The command runner uses per-job keys and a loopback result socket. It accepts
no further commands through that socket. This is not encryption for SDB.

Setup rejects unknown builds, pending updates and unreviewed package IDs.
It requires confirmation, takes metadata backups and leaves its lock in place
after an incomplete apply. See [recovery](docs/debloat.md#interrupted-apply).
It never updates firmware, reboots or automatically retries a failed operation.

Reports, backups, screenshots and command output may contain account or viewing
data. Keep them private. Only remove staging belonging to your own named job.

The public root entry point and bootstrap are credited in [THIRD_PARTY.md](THIRD_PARTY.md).
No new vulnerability or coordinated disclosure is claimed.
