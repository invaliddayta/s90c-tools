#!/usr/bin/env python3
"""Read package, guard and update state. Save private snapshots for comparison."""
import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys

HERE = Path(__file__).resolve().parent
ACCESS = HERE.parent / 'tv_access' / 'tv-access'
CATALOG = HERE.parents[1] / 'profiles/s90c.json'
APP_ID = 'org.tizen.was-appsync'
PROTECTED = {APP_ID, 'com.samsung.tv.cobalt', 'org.tizen.primevideo', 'org.tizen.netflix-app'}
UPDATE_TYPES = {
    'db/menu/support/softwareupdate/autoupdate': 'Int32',
    'db/SyncML/ivi-oma-dm-service/software-update/autoupdate/setting': 'Int32',
    'db/apps/auto_update': 'Boolean',
    'db/built-in-app-upgrader/server_upgrade': 'Boolean',
}
PENDING = ('db/menu/support/softwareupdate/reboot',
           'db/menu/support/softwareupdate/upgrade_verified_successfully',
           'db/menu/support/softwareupdate/upgrade_finalization_schedule')
KEYS = (*UPDATE_TYPES, *PENDING)
COMMAND = r'''set -e
printf '\n@@BOOT\n'
head -1 /proc/sys/kernel/random/boot_id
head -1 /proc/uptime
grep '^btime ' /proc/stat
printf '\n@@PACKAGES\n'
pkgcmd -l
printf '\n@@GUARD\n'
sqlite3 -readonly -bail -cmd '.timeout 1500' /opt/dbspace/.pkgmgr_parser.db "SELECT app_id,app_disable,app_support_disable,app_exec FROM package_app_info WHERE app_id='org.tizen.was-appsync'; SELECT count(*) FROM package_app_info_for_uid WHERE app_id='org.tizen.was-appsync';"
printf '\n@@FIRMWARE\n'
sha256sum /etc/os-release
grep '^BUILD_ID=' /etc/os-release
printf '\n@@SETTINGS\n'
for k in KEY_LIST; do vconftool get "$k"; done
printf '\n@@BOOT_END\n'
head -1 /proc/sys/kernel/random/boot_id
printf '\n@@END\n'
'''.replace('KEY_LIST', ' '.join(KEYS))


def catalog_ids():
    rows = json.loads(CATALOG.read_text())['packages']
    values = [row['id'] for row in rows]
    if (any(not isinstance(v, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+', v)
            for v in values) or len(set(values)) != len(values) or
            set(values).intersection(PROTECTED)):
        raise ValueError('Invalid bundled package catalog')
    return values


def parse_snapshot(output):
    sections, current = {}, None
    for line in output.splitlines():
        if line.startswith('@@'):
            current = line[2:]
            if current in sections:
                raise ValueError('Duplicate snapshot section: ' + current)
            sections[current] = []
        elif current is not None and line.strip():
            sections[current].append(line.strip())
    required = {'BOOT', 'PACKAGES', 'GUARD', 'FIRMWARE', 'SETTINGS', 'BOOT_END', 'END'}
    if set(sections) != required:
        raise ValueError('Incomplete/unexpected snapshot sections')
    boot = sections['BOOT']
    uuid = r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}'
    if len(boot) != 3 or not re.fullmatch(uuid, boot[0]):
        raise ValueError('Invalid boot-ID/uptime evidence')
    if sections['BOOT_END'] != [boot[0]]:
        raise ValueError('Boot changed during collection; snapshot is inconsistent')
    uptime = float(boot[1].split()[0])
    btime = re.fullmatch(r'btime (\d+)', boot[2])
    if not btime or not math.isfinite(uptime) or uptime < 0:
        raise ValueError('Invalid uptime/btime evidence')
    packages = {}
    for line in sections['PACKAGES']:
        m = re.search(r'pkgid \[([^\]]+)\].*?version \[([^\]]+)\]', line)
        if m:
            if m[1] in packages:
                raise ValueError('Duplicate package: ' + m[1])
            packages[m[1]] = m[2]
        elif 'pkgid [' in line:
            raise ValueError('Unparsed package record')
    if not packages:
        raise ValueError('Empty package inventory')
    rows = sections['GUARD']
    if len(rows) != 2 or len(rows[0].split('|')) != 4:
        raise ValueError('Missing/ambiguous guard row')
    app, disabled, support, executable = rows[0].split('|')
    if app != APP_ID or disabled not in ('true', 'false') or support not in ('true', 'false'):
        raise ValueError('Unexpected guard metadata')
    overrides = int(rows[1])
    firmware = sections['FIRMWARE']
    if len(firmware) != 2 or not re.fullmatch(r'[0-9a-f]{64}\s+/etc/os-release', firmware[0]):
        raise ValueError('Missing firmware identity hash')
    if not firmware[1].startswith('BUILD_ID='):
        raise ValueError('Missing firmware build ID')
    settings = {}
    for line in sections['SETTINGS']:
        m = re.fullmatch(r'(.+), value = (.*) \((Boolean|Int32)\)', line)
        if not m or m[1] not in KEYS or m[1] in settings:
            raise ValueError('Invalid settings readback')
        value = m[2]
        if m[3] == 'Int32':
            value = int(value)
        else:
            if value not in ('True', 'False'):
                raise ValueError('Invalid Boolean readback')
            value = value == 'True'
        settings[m[1]] = {'type': m[3], 'value': value}
    if set(settings) != set(KEYS):
        raise ValueError('Missing settings readbacks')
    return {
        'schema': 2, 'captured_at_utc': datetime.now(timezone.utc).isoformat(),
        'boot_id': boot[0], 'boot_time_epoch': int(btime[1]), 'uptime_seconds': uptime,
        'packages': packages, 'package_count': len(packages),
        'catalog_present': sorted(set(catalog_ids()).intersection(packages)),
        'guard': {'app_id': app, 'disabled': disabled == 'true',
                  'support_disable': support == 'true', 'exec': executable,
                  'uid_override_rows': overrides},
        'firmware_build': firmware[1].split('=', 1)[1].strip('"'),
        'os_release_sha256': firmware[0].split()[0], 'settings': settings,
    }


def compare(before, after):
    if before.get('schema') != 2 or after.get('schema') != 2:
        raise ValueError('Unsupported snapshot schema; collect a new baseline with this version')
    a, b = before['packages'], after['packages']
    return {
        'boot_changed': before['boot_id'] != after['boot_id'],
        'packages_added': sorted(set(b) - set(a)),
        'packages_removed': sorted(set(a) - set(b)),
        'versions_changed': {p: [a[p], b[p]] for p in sorted(set(a) & set(b)) if a[p] != b[p]},
        'os_release_changed': before['os_release_sha256'] != after['os_release_sha256'],
        'guard_changed': before['guard'] != after['guard'],
        'settings_changed': {k: [before['settings'].get(k), v] for k, v in after['settings'].items()
                             if before['settings'].get(k) != v},
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='permit readbacks from the TV')
    parser.add_argument('--tv', help='TV host; otherwise TV_HOST/local configuration')
    parser.add_argument('--output', required=True, type=Path, help='new private snapshot file')
    parser.add_argument('--baseline', type=Path, help='previous snapshot for comparison')
    args = parser.parse_args(argv)
    if not args.live:
        parser.error('No TV access without --live')
    if args.output.exists():
        parser.error('Output exists; choose a new filename')
    try:
        before = json.loads(args.baseline.read_text()) if args.baseline else None
        if before is not None and (not isinstance(before, dict) or before.get('schema') != 2):
            raise ValueError('Unsupported snapshot schema; collect a new baseline with this version')
        command = [str(ACCESS), '--timeout', '25']
        if args.tv is not None:
            command.extend(['--tv', args.tv])
        command.append(COMMAND)
        run = subprocess.run(command, capture_output=True, text=True, timeout=90)
        if run.returncode:
            raise RuntimeError('tv-access failed (not retried): ' + run.stderr[-1500:])
        result = parse_snapshot(run.stdout)
        delta = compare(before, result) if before else None
        args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as out:
            json.dump(result, out, indent=2, sort_keys=True)
            out.write('\n')
        summary = {k: result[k] for k in ('package_count', 'catalog_present', 'guard')}
        if delta is not None:
            summary['compared_with_baseline'] = delta
        print(json.dumps(summary, indent=2, sort_keys=True))
        print('Private snapshot:', args.output)
        return 0
    except (OSError, ValueError, KeyError, IndexError, RuntimeError, subprocess.TimeoutExpired) as error:
        print('check:', error, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
