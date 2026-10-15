#!/usr/bin/env python3
"""Preview, apply or verify S90C app cleanup and update settings."""
import argparse
import base64
import copy
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import uuid

from app_sync_guard import check
from tv_access import tv_config as config

ROOT = Path(__file__).resolve().parent.parent
BUILD = 'T-PTMCDEUC-REL-202408210126'
GUARD_EXEC = '/usr/apps/org.tizen.was-appsync/bin/was-appsync'
UPDATES = check.UPDATE_TYPES
PENDING = check.PENDING
DEFAULT_PROFILE = ROOT / 'profiles/s90c.json'
REMOTE_BASE = '/opt/var/tv-hardening/tv-setup'
REMOTE_LOCK = REMOTE_BASE + '/active'
EXTRA = r'''
printf '\n@@SETUP_META\n'
printf 'uid='; id -u
printf 'arch='; uname -m
printf 'model='; /usr/bin/fconfigd -m
printf 'profile='; /usr/bin/fconfigd -l
printf 'owner='; grep '^owner:' /proc/1/root/etc/passwd | cut -d: -f3,4
printf 'pending='
if [ -e /mnt/backup/OS_UPGRADE_FLAG ] || [ -e /mnt/systemrw/upgrade/.key ]; then echo yes; else echo no; fi
command -v pidof >/dev/null
printf 'appsync='
pidof was-appsync || test "$?" -eq 1
printf '\n@@SETUP_END\n'
head -1 /proc/sys/kernel/random/boot_id
'''
READ_COMMAND = 'set -eu\ntest -f /opt/dbspace/.pkgmgr_parser.db\n' + check.COMMAND + EXTRA


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON field: ' + key)
        result[key] = value
    return result


def load_profile(path):
    if path.stat().st_size > 131072:
        raise ValueError('Profile exceeds 128 KiB')
    data = json.loads(path.read_text(), object_pairs_hook=unique_object)
    if (not isinstance(data, dict) or set(data) != {'schema', 'name', 'packages'} or
            type(data['schema']) is not int or data['schema'] != 1 or
            not isinstance(data['packages'], list)):
        raise ValueError('Profile needs schema=1, name and packages')
    allowed, seen = set(check.catalog_ids()), set()
    for row in data['packages']:
        if not isinstance(row, dict) or set(row) != {'id', 'name', 'reason', 'consequence', 'remove'}:
            raise ValueError('Each package needs id, name, reason, consequence, remove')
        package = row['id']
        if not isinstance(package, str) or package not in allowed or package in seen:
            raise ValueError('Unknown/protected/duplicate package ID: ' + str(package))
        seen.add(package)
        if type(row['remove']) is not bool:
            raise ValueError('remove must be a JSON boolean, not a string/number')
        for key in ('name', 'reason', 'consequence'):
            if not isinstance(row[key], str) or not 1 <= len(row[key]) <= 1000 or not row[key].isprintable():
                raise ValueError('Profile text must be nonempty, printable and bounded')
    if not isinstance(data['name'], str) or not 1 <= len(data['name']) <= 160 or not data['name'].isprintable():
        raise ValueError('Invalid profile name')
    return data


def parse_readback(output):
    basic, extra = output.split('\n@@SETUP_META\n')
    metadata, end = extra.split('\n@@SETUP_END\n')
    state = check.parse_snapshot(basic)
    fields = [line.split('=', 1) for line in metadata.strip().splitlines()]
    if any(len(line) != 2 for line in fields) or len(dict(fields)) != len(fields):
        raise ValueError('Malformed setup metadata')
    state['setup'] = dict(fields)
    if end.strip() != state['boot_id']:
        raise ValueError('Boot changed during setup readback')
    return state


def preflight(state):
    expected = {'uid': '0', 'arch': 'armv7l', 'model': '1150', 'profile': '9',
                'owner': '5001:100', 'pending': 'no', 'appsync': ''}
    if state['firmware_build'] != BUILD:
        raise ValueError('Unsupported firmware build; never update/downgrade to force compatibility')
    if set(state['setup']) != set(expected):
        raise ValueError('Unexpected setup metadata fields')
    for key, value in expected.items():
        if state['setup'][key] != value:
            if key == 'pending':
                raise ValueError('Upgrade marker present; do not clear it, scan or reboot')
            if key == 'appsync':
                raise ValueError('App-sync is running; wait and inspect before maintenance, do not kill it blindly')
            raise ValueError('Unsupported %s: expected %s, got %s' % (key, value, state['setup'][key]))
    if 'com.samsung.tv.cobalt' not in state['packages']:
        raise ValueError('Shared Cobalt is missing; this is not the supported baseline')
    guard = state['guard']
    if (guard['exec'] != GUARD_EXEC or guard['support_disable'] or guard['uid_override_rows'] != 0):
        raise ValueError('Unexpected guard metadata; do not override it')
    for key, kind in UPDATES.items():
        item = state['settings'][key]
        if item['type'] != kind or type(item['value']) is not (bool if kind == 'Boolean' else int) or item['value'] not in (0, 1):
            raise ValueError('Unexpected update-setting type/value: ' + key)
    for key in PENDING:
        if state['settings'][key] != {'type': 'Int32', 'value': 0}:
            raise ValueError('Pending upgrade state; do not scan, clear flags or reboot')


def make_plan(state, profile, policies_only=False):
    preflight(state)
    selected = [row for row in profile['packages'] if row['remove']] if not policies_only else []
    changed_updates = [key for key in UPDATES if state['settings'][key]['value']]
    actions = [{'kind': 'updates', 'keys': changed_updates}] if changed_updates else []
    guard_needed = bool(selected)
    if guard_needed and not state['guard']['disabled']:
        actions.append({'kind': 'guard'})
    actions += [{'kind': 'remove', 'id': row['id']} for row in selected if row['id'] in state['packages']]
    return {'actions': actions, 'selected': selected, 'guard_needed': guard_needed,
            'policies_only': policies_only}


def private_json(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as out:
        json.dump(data, out, indent=2, sort_keys=True)
        out.write('\n')


class TV:
    def __init__(self, host, report):
        self.host, self.report, self.sequence = host, report, 0

    def run(self, command, label, timeout=30):
        if len(command.encode()) > 65536 or '\0' in command:
            raise ValueError('Command exceeds transport bounds')
        self.sequence += 1
        log = self.report / ('%03d-%s.json' % (self.sequence, label))
        # Intent recorded BEFORE submission; failed/lost results must not be replayed.
        private_json(log, {'operation': label, 'submitted_command': command, 'tv': self.host})
        result = subprocess.run([str(check.ACCESS), '--tv', self.host, '--timeout', str(timeout), command],
                                capture_output=True, text=True, timeout=timeout + 75)
        private_json(log.with_suffix('.result.json'), {'exit': result.returncode,
                     'stdout': result.stdout, 'stderr': result.stderr})
        if result.returncode or 'output capped' in result.stderr or 'background children' in result.stderr:
            raise RuntimeError('%s failed (exit %s), NOT retried. Inspect %s before further writes' %
                               (label, result.returncode, self.report))
        return result.stdout

    def snapshot(self):
        state = parse_readback(self.run(READ_COMMAND, 'readback'))
        preflight(state)
        private_json(self.report / ('%03d-snapshot.json' % self.sequence), state)
        return state


def stable(before, after):
    """Allow only the expected boot, firmware, package, setting and guard state."""
    keys = ('boot_id', 'boot_time_epoch', 'firmware_build', 'os_release_sha256',
            'packages', 'settings', 'guard', 'setup')
    for key in keys:
        if before[key] != after[key]:
            raise RuntimeError('Unexpected state change: ' + key + '; stop and inspect private reports')


def guard_prefix(state):
    # Each mutating job rechecks boot/firmware/pending flags on the TV, not only
    # in a host snapshot that might predate a reboot or a changed DHCP target.
    pending = ''.join('test "$(vconftool get ' + key + ')" = ' +
                      shlex.quote(key + ', value = 0 (Int32)') + '\n' for key in PENDING)
    return ('set -eu\numask 077\n' + pending +
            'if pidof was-appsync >/dev/null; then exit 1; else test "$?" -eq 1; fi\n'
            'test "$(id -u)" = 0\n'
            'test "$(head -1 /proc/sys/kernel/random/boot_id)" = ' + shlex.quote(state['boot_id']) + '\n'
            'test "$(sha256sum /etc/os-release | cut -d\' \' -f1)" = ' + shlex.quote(state['os_release_sha256']) + '\n'
            'test ! -e /mnt/backup/OS_UPGRADE_FLAG\ntest ! -e /mnt/systemrw/upgrade/.key\n')


def helper_files(guard):
    if not guard:
        return {}
    base = ROOT / 'tools/app_sync_guard'
    hashes = dict(line.split(maxsplit=1)[::-1] for line in (base / 'SHA256SUMS').read_text().splitlines())
    result = {}
    for name in ('PkgInfoProbe.cs', 'build/PkgInfoProbe.dll', 'build/PkgInfoProbe.runtimeconfig.json'):
        path = base / name
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != hashes[name]:
            raise ValueError('Missing/mismatched helper: %s. Build and verify it before applying' % path)
        if name.startswith('build/'):
            result[path.name] = path.read_bytes()
    return result


def native_guard(tv, prefix, directory, disabled):
    output = tv.run(prefix + 'dotnet ' + directory + '/PkgInfoProbe.dll 5001', 'native-guard')
    required = ['uid=5001 appinfo rc=' + ('-3' if disabled else '0'),
                'exec rc=0 value=' + GUARD_EXEC, 'support rc=0 value=0',
                'disabled rc=0 value=' + ('1' if disabled else '0')]
    if disabled:
        required.append('disabled appinfo rc=0')
    if any(output.splitlines().count(line) != 1 for line in required):
        raise RuntimeError('Native guard readback differs from DB; no automatic cache reset for an existing guard')


def show_plan(plan, state):
    print('Updates: disable both firmware and both app auto-update settings (no scan).')
    if plan['guard_needed']:
        print('Restoration guard:', 'already set' if state['guard']['disabled'] else 'will enable and refresh BOTH metadata caches')
    for row in plan['selected']:
        if row['id'] in state['packages']:
            print('REMOVE %s [%s]\n  %s' % (row['name'], row['id'], row['consequence']))
    missing = sum(row['id'] not in state['packages'] for row in plan['selected'])
    print('%d selected packages already absent. %d configuration/removal actions.' % (missing, len(plan['actions'])))
    print('No firmware update or reboot.')


def apply(tv, state, plan, files):
    if not plan['actions']:
        print('Already configured; no setters, cache refresh or uninstall performed.')
        return
    stable(state, tv.snapshot())  # Confirmation may have been open for a while.
    prefix = guard_prefix(state)
    directory = REMOTE_BASE + '/' + uuid.uuid4().hex
    private_json(tv.report / 'remote-workspace.json', {'directory': directory, 'lock': REMOTE_LOCK})
    # Persistent, fail-closed lock across hosts. Never steal/remove a stale lock
    # automatically: an interrupted pkgcmd operation may still be running.
    tv.run(prefix + 'mkdir -p ' + REMOTE_BASE + '\nmkdir ' + REMOTE_LOCK + '\n'
           'printf %s ' + shlex.quote(directory) + ' > ' + REMOTE_LOCK + '/workspace\n'
           'mkdir ' + directory + '\n'
           'sqlite3 -bail -cmd ".timeout 2000" /opt/dbspace/.pkgmgr_parser.db ".backup ' + directory + '/pkgmgr-before.db"\n'
           'cp /opt/var/log/appfw/app-installers/installation-history.log ' + directory + '/history-before', 'backup-lock')
    if files:
        lines = [prefix]
        for name, data in files.items():
            target = directory + '/' + name
            lines += ['printf %s ' + shlex.quote(base64.b64encode(data).decode()) + ' | base64 -d > ' + target,
                      'printf \'%s\\n\' ' + shlex.quote(hashlib.sha256(data).hexdigest() + '  ' + target) + ' | sha256sum -c -']
        tv.run('\n'.join(lines), 'stage-helpers')
    stable(state, tv.snapshot())  # Also catches changes while the confirmation was open.
    if plan['guard_needed']:
        native_guard(tv, prefix, directory, state['guard']['disabled'])
    for action in plan['actions']:
        stable(state, tv.snapshot())
        expected = copy.deepcopy(state)
        kind = action['kind']
        if kind == 'updates':
            # Treat linked settings as one bundle: a daemon may mirror one key
            # into another. Verify all originals before starting, then all four
            # desired typed values afterward. This is not an atomic transaction.
            lines = []
            for key, kind_name in UPDATES.items():
                original = str(state['settings'][key]['value'])
                lines.append('test "$(vconftool get ' + key + ')" = ' +
                             shlex.quote(key + ', value = ' + original + ' (' + kind_name + ')'))
            for key in action['keys']:
                desired = 'False' if UPDATES[key] == 'Boolean' else '0'
                lines.append('if [ "$(vconftool get ' + key + ')" != ' +
                             shlex.quote(key + ', value = ' + desired + ' (' + UPDATES[key] + ')') +
                             ' ]; then vconftool set -f -t ' + ('bool' if UPDATES[key] == 'Boolean' else 'int') + ' ' + key + ' 0; fi')
                expected['settings'][key]['value'] = False if UPDATES[key] == 'Boolean' else 0
            command = '\n'.join(lines)
        elif kind == 'guard':
            sql = (ROOT / 'tools/app_sync_guard/apply.sql').read_text()
            command = ('printf %s ' + shlex.quote(sql) + ' | sqlite3 -bail -cmd ".timeout 2000" /opt/dbspace/.pkgmgr_parser.db\n'
                       'systemctl restart pkgmgr-info.service\nsleep 5\n'
                       'dotnet ' + directory + '/PkgInfoProbe.dll owner-reload')
            expected['guard']['disabled'] = True
        elif kind == 'remove':
            package = action['id']
            # Check the native guard immediately before EVERY removal. No owner
            # launch probe: a failed guard must not start restoration as a test.
            native_guard(tv, prefix, directory, True)
            guarded = ("SELECT count(*) FROM package_app_info WHERE app_id='org.tizen.was-appsync' "
                       "AND app_disable='true' AND app_support_disable='false' AND app_exec='" + GUARD_EXEC +
                       "' AND NOT EXISTS(SELECT 1 FROM package_app_info_for_uid WHERE app_id='org.tizen.was-appsync');")
            command = 'test "$(sqlite3 -readonly -bail /opt/dbspace/.pkgmgr_parser.db ' + shlex.quote(guarded) + ')" = 1\n'
            for key, kind_name in UPDATES.items():
                desired = 'False' if kind_name == 'Boolean' else '0'
                command += 'test "$(vconftool get ' + key + ')" = ' + shlex.quote(key + ', value = ' + desired + ' (' + kind_name + ')') + '\n'
            command += 'pkgcmd -u -n ' + shlex.quote(package)
            del expected['packages'][package]
        else:
            raise ValueError('Unknown action')
        print('Applying:', kind, action.get('id', action.get('key', '')), flush=True)
        output = tv.run(prefix + command, kind, timeout=60)
        if kind == 'guard' and any(output.splitlines().count(line) != 1 for line in
                                   ('caller uid=5001', 'aul_reload_appinfo rc=0')):
            raise RuntimeError('Owner cache refresh did not report the expected identity/result')
        after = tv.snapshot()
        stable(expected, after)
        if kind == 'guard':
            native_guard(tv, prefix, directory, True)
        state = after
    # Do not delete backups or helper files. Only release OUR lock on fully
    # verified completion. Any exception above intentionally leaves it in place.
    tv.run(prefix + 'test "$(head -c 200 ' + REMOTE_LOCK + '/workspace)" = ' + directory + '\n'
           'rm ' + REMOTE_LOCK + '/workspace\nrmdir ' + REMOTE_LOCK, 'unlock')
    private_json(tv.report / 'completed.json', {'verified': True, 'remaining_actions': 0})
    print('Selected settings/removals verified. Guard launch rejection and reboot/playback acceptance are separate.')


def main(argv=None):
    parser = argparse.ArgumentParser(prog='tv-setup', description=__doc__)
    parser.add_argument('operation', choices=('plan', 'apply', 'verify'))
    parser.add_argument('--tv', help='TV host; otherwise TV_HOST/local tv-access configuration')
    parser.add_argument('--profile', type=Path, default=DEFAULT_PROFILE, help='reviewed JSON selection (default: bundled conservative profile)')
    parser.add_argument('--policies-only', action='store_true', help='update settings only; no guard/uninstalls')
    parser.add_argument('--yes', action='store_true', help='apply: explicit consent without interactive prompt; TV must not be in use')
    parser.add_argument('--reports', type=Path, default=Path('reports'), help='PRIVATE report parent; creates a fresh subdirectory')
    if argv == [] or (argv is None and len(sys.argv) == 1):
        parser.print_help()
        return 0
    args = parser.parse_args(argv)
    if args.yes and args.operation != 'apply':
        parser.error('--yes is only for apply')
    try:
        profile = load_profile(args.profile)
        host = config.resolve_host(args.tv)
        args.reports.mkdir(parents=True, exist_ok=True, mode=0o700)
        report = args.reports / ('setup-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex[:8])
        report.mkdir(mode=0o700)
        private_json(report / 'profile.json', profile)
        print('Private report:', report, flush=True)
        tv = TV(host, report)
        state = tv.snapshot()
        plan = make_plan(state, profile, args.policies_only)
        private_json(report / 'plan.json', plan)
        show_plan(plan, state)
        if args.operation == 'plan':
            print('Preview only. No policy setters, cache refresh or uninstall.')
            return 0
        if args.operation == 'verify':
            print('Selected desired state:', 'MATCHES' if not plan['actions'] else 'NOT MET')
            return 0 if not plan['actions'] else 1
        if plan['actions'] and not args.yes:
            if not sys.stdin.isatty():
                raise ValueError('Apply needs interactive confirmation or --yes; nothing changed')
            answer = input('TV not in use? Accept irreversible removals/cache refresh and type APPLY: ')
            if answer != 'APPLY':
                print('Cancelled. Nothing changed.')
                return 0
        files = helper_files(plan['guard_needed']) if plan['actions'] else {}
        # The TV lock covers other hosts. This additional host lock gives a clear
        # early failure for two local apply invocations, without nesting SDB locks.
        cache = Path(os.environ.get('XDG_CACHE_HOME', Path.home() / '.cache')) / 'tv-setup'
        cache.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(cache / 'apply.lock', os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            apply(tv, state, plan, files)
        finally:
            os.close(fd)
        return 0
    except KeyboardInterrupt:
        print('Interrupted. A TV job may still run until its deadline. Inspect reports/lock before any retry.', file=sys.stderr)
        return 130
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.TimeoutExpired) as error:
        print('tv-setup:', error, file=sys.stderr)
        if args.operation == 'apply':
            print('Stopped. No automatic retry/rollback. Inspect private reports and any TV setup lock before another apply.', file=sys.stderr)
        else:
            print('Read-only operation failed. No setup setters, cache refresh or uninstalls were requested.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
