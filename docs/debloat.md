# App cleanup

These tools change package metadata and uninstall apps.
Use a maintenance window when nobody is watching.
The guard's individual controls were tested on the TV. The full automated apply
workflow has only offline tests, not an end-to-end live acceptance run.

## Selection

Copy `profiles/s90c.json` to `local/my-tv.json` and edit `remove`.
The bundled catalog contains 46 reviewed IDs. The default selects 24 advertising
components and Ambient themes. Streaming apps and device integrations are kept.
Ambient themes are decorative. Removing them does not change the protective screensaver.

Read each consequence. Removing an app can delete its data.
TizenTube is not installed by this toolkit. Shared Cobalt and packages outside
the trusted bundled catalog cannot be selected through a profile.
New IDs require editing and reviewing that catalog, not just an imported profile.

```sh
nix run .#tv-setup -- plan --profile local/my-tv.json
nix run .#tv-setup -- apply --profile local/my-tv.json
nix run .#tv-setup -- verify --profile local/my-tv.json
```

Without Nix, build the guard helper as described in [usage](usage.md#builds-and-tests)
and use `python3 tools/tv-setup.py` with the same arguments.

Apply re-reads the TV and prints its own plan before asking you to type `APPLY`.
`--yes` skips that prompt and is only for an explicitly authorized unattended run.
Verify exits 0 when selected settings/inventory match, otherwise 1. It does not
fix drift or prove playback, DRM or every possible restoration path.

## Update settings only

```sh
nix run .#tv-setup -- plan --policies-only
nix run .#tv-setup -- apply --policies-only
nix run .#tv-setup -- verify --policies-only
```

This disables both known firmware-auto-update keys and both app-auto-update keys,
without uninstalling apps or enabling the restoration guard.
Retained apps lose automatic updates too. These settings do not guarantee
suppression of upgrade offers. No updater scan or download is triggered.

## Restoration guard

App auto-update settings alone did not stop removed apps returning on the test TV.
The guard changes only `package_app_info.app_disable` to TEXT `true` for
`org.tizen.was-appsync`. The executable and support flag stay original.
There must be no per-user override rows.

Two metadata caches need refreshing: restart `pkgmgr-info.service`, then call
`aul_reload_appinfo()` as UID/GID 5001. The setup command does both after a new
guard change, and reads the native state before each uninstall.
It does not refresh an existing guard just to check it.

The original test rejected an owner launch with exit 255 and no app-sync process.
Later boots retained the guard and 45 of 46 removed apps stayed absent. Spotify
returned for an unknown reason. This is not a ban on every installer.
Store onboarding or sync features may be affected.

## What apply checks

Apply requires the recorded firmware build, ARMv7, model/profile 1150/9,
owner 5001:100, no pending upgrade and no running app-sync process.
It rejects foreign guard metadata and unknown update-setting values.

It saves a private host baseline and acquires a TV-side lock, then backs up the
package database and installer history. Each step checks boot identity, firmware,
inventory, typed settings and guard state. It stops on unexpected changes.
The package manager performs removals. No app directories are force-deleted.

Reports are mode 0600 in a new mode 0700 directory under `reports/`.
`--reports PATH` changes the parent. Helpers/backups remain in a unique TV directory
under `/opt/var/tv-hardening/tv-setup/`. Keep these files private.
No automatic reboot, retry or rollback is performed.

## Interrupted apply

The active TV lock is `/opt/var/tv-hardening/tv-setup/active`.
Its `workspace` file and the host's `remote-workspace.json` identify the run.

1. Stop after a timeout or lost result. The command may have completed.
2. Inspect the recorded operation, current boot, inventory and installer history.
3. Confirm no setup/installer is still running before removing the lock's
   `workspace` file and its now-empty `active` directory. Keep the backups.
4. If a guard cache refresh was interrupted, inspect native state and finish the
   two-cache sequence below, or use the narrow rollback.
5. Review a fresh plan before another apply. Already-correct values are skipped.

The tool does not steal stale locks.

## Guard rollback

Only undo a guard this run changed from false to true. Check the saved baseline
and current row first. Do not restore an old whole database over the live one.
The SQL asserts the original executable/support flag and absence of UID overrides.
Use `sqlite3 -bail` so a failed assertion aborts before commit.

Run from a checkout with dependencies available, after replacing `YOUR_JOB` with
the exact helper directory from the private report:

```sh
python3 - <<'PY'
from pathlib import Path
import shlex
import subprocess
job = '/opt/var/tv-hardening/tv-setup/YOUR_JOB'
if job.endswith('/YOUR_JOB'):
    raise SystemExit('Set the recorded job directory first')
sql = Path('tools/app_sync_guard/rollback.sql').read_text()
command = ('set -e\nprintf %s ' + shlex.quote(sql) +
           ' | sqlite3 -bail /opt/dbspace/.pkgmgr_parser.db\n' +
           'systemctl restart pkgmgr-info.service\nsleep 5\n' +
           'dotnet ' + shlex.quote(job + '/PkgInfoProbe.dll') + ' owner-reload\n' +
           'dotnet ' + shlex.quote(job + '/PkgInfoProbe.dll') + ' 5001')
subprocess.run(['./tools/tv_access/tv-access', command], check=True)
PY
```

For an interrupted refresh, use the same restart/reload/read sequence without
the SQL. After rollback, native metadata must show disabled=0, support=0 and the
original executable. Readback is not a launch test. Re-enabling app-sync may allow
apps to return. App reinstall/data recovery is not guaranteed by the backups.
