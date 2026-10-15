# Usage

## Developer Mode

Use the model and software version shown in About This TV, not an update check.
The tested build is in the README.

1. Open Smart Hub, then Apps.
2. Enter `12345` with the remote's number keys.
3. Enable Developer Mode and allow your **computer's** LAN IP.
4. If a restart is requested, check that no firmware update is pending first.
   Arrange a normal physical restart. Do not use `systemctl reboot --force`.

[Samsung's instructions](https://developer.samsung.com/smarttv/develop/getting-started/using-sdk/tv-device.html)
describe the dialog. Do not follow their recommendation to update firmware.
Changing the TV or computer's DHCP address may break access.

## Local installation

Python 3, Bash and OpenSSL are needed for root access.
ImageMagick is only needed for resized screenshots. Mono is needed to build the
cleanup helper, not to run the shipped root-access binaries.

You can use Nix without installation: `nix run .#tv-access -- 'id -u'`.
Otherwise run the wrappers directly, or install symlinks from a checkout:

```sh
export TV_HOST=192.0.2.10  # replace with your TV's address
python3 tools/tv_access/install.py --tv "$TV_HOST"
```

This writes local config and symlinks, not TV settings. Keep the checkout in place.
Existing conflicting files are refused unless you explicitly pass `--force`.
Use `--bin-dir` for a different destination or `--skill-dir` to install the skill.

Target precedence: `--tv HOST`, `TV_HOST`, then `tv_host` in
`$XDG_CONFIG_HOME/tv-access/config.json` (normally `~/.config`).
`TV_ACCESS_CONFIG` overrides that file. There is no discovery or default TV.

## Commands

```sh
tv-access --probe
tv-access 'id -u'
tv-access --timeout 90 'df -h /opt'
tv-access 'head -c 65536 /dev/zero' > /tmp/bytes.bin
tv-key XF86Home Down*3 Return sleep:2
tv-shot /tmp/tv.png
tv-shot /tmp/full.png --full
```

`--probe` checks the full root path and reports the build. A handshake alone
does not prove root. Require `id -u` to print 0 before doing further work.

One quoted shell command is supported, including pipes and multiline text.
There is no interactive TTY or streamed stdin. Normal setup takes about 3-4 seconds.
The command deadline defaults to 45 seconds, with `--timeout` from 1 to 300.
stdout and stderr are separate and binary-safe, capped at 1 MiB each.

The command's exit status is returned. Timeout is 124, transport failure 125,
interrupt 130. Commands can return these values themselves, so read diagnostics.
Timeout kills the process group. Detached background processes must close stdin,
stdout and stderr if they are to outlive the command.

Keys are sent 0.6 seconds apart. Screenshots capture the compositor, not protected
video. Resized output is 960 pixels wide. `--full` needs no image converter.
PNGs above 1 MiB are rejected.

## How root works

The client stages a signed archive, bootstrap and command over SDB.
The public appinstall entry point runs Samsung's signed dotnet host.
Chris's bootstrap uses a private mount namespace and Samsung's archive launcher
to start the job as UID 0. Global account/key files are not rewritten.

A run-once guard prevents duplicate execution. The runner returns cached stdout,
stderr, status, flags and UID through a temporary loopback socket forwarded by SDB.
A fresh 32-byte key authenticates retrieval. It cannot accept new commands.
Acknowledgment removes the job, or it expires 90 seconds after completion.
Failures before the runner starts can leave their named staging directory.

On the tested TV, root/results, exact 64 KiB output, timeouts and a single-execution
counter were checked on October 9, 2026. A normal reboot changed the boot ID,
left firmware unchanged and restored SDB in about 25 seconds. Root commands then
worked without a separate re-root step. This does not cover factory reset or
every cold-power, DHCP and firmware combination.

## Troubleshooting

Check power, target address, Developer Mode and the allowed computer IP first.
Another SDB client can interfere. Inspect connections rather than killing it blindly.
Only handshake resets before a request are retried. Submitted commands are not.
After a lost result, inspect effects and only that job's staging before further writes.

To uninstall locally, remove the symlinks and config you installed.
No custom TV boot service needs removal. Command side effects are separate.

## Builds and tests

```sh
nix develop --no-write-lock-file
nix flake check --no-write-lock-file
```

The flake supports aarch64-linux and x86_64-linux. Offline tests do not contact a TV.
For non-Nix cleanup, build and verify the guard helper first:

```sh
bash tools/app_sync_guard/build.sh
(cd tools/app_sync_guard && sha256sum -c SHA256SUMS)
```

Root-access rebuilds use `tools/tv_access/build.sh --runner-only` (Mono) or
`--bootstrap-only` (.NET SDK with net6.0 support). Builds do not rewrite hash manifests.
See [binary provenance](../THIRD_PARTY.md) before replacing shipped artifacts.

The opt-in `tools/tv_access/tests/live_smoke.py --live --tv HOST` checks root,
streams, binary output, timeout and a temporary counter. It writes temporary files
and requires separate approval. No test reboots, uninstalls apps or updates firmware.
