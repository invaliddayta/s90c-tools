---
name: tv-access
description: Run root commands on an authorized Samsung S90C over SDB. Returns stdout, stderr and exit status. Reacquires root per command after reboot. Requires the TV powered on and Developer Mode configured.
---

# TV access

Read the repository's `SECURITY.md` before use. Target selection is `--tv HOST`,
`TV_HOST`, then `~/.config/tv-access/config.json` (`tv_host`).
Do not guess or scan for a TV.

```sh
tv-access --probe
tv-access 'id; uname -a'
tv-access --timeout 90 'your command'
tv-key XF86Home
tv-shot /tmp/tv.png
```

- Never update, downgrade or flash firmware. Reboots need separate permission.
  Do not use `systemctl reboot --force`.
- Root access is not permission to apply a cleanup profile or change settings.
- One quoted shell command, no TTY or streamed stdin. Pipes work.
- Default deadline 45 seconds, range 1-300. Allow at least 60 seconds extra
  setup time in the calling harness. Output cap is 1 MiB per stream.
- Timeout returns 124 and kills the process group. Transport failure returns 125.
  A lost result can mean the command ran. Inspect effects before retrying.
- Root is reacquired per command after SDB returns. No separate re-root step.
- Keys can interrupt viewing. Screenshots can expose account data and omit
  protected video. Keep output private.
- For app cleanup and interrupted applies, follow `docs/debloat.md`.
