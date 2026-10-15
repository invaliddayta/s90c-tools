"""Offline setup orchestration tests. No TV, SDB socket, setter or uninstall runs."""
import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools'))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


setup = load('tv_setup', 'tools/tv-setup.py')
fixtures = load('guard_fixtures', 'tools/app_sync_guard/tests/test_guard.py')


def wire():
    metadata = 'uid=0\narch=armv7l\nmodel=1150\nprofile=9\nowner=5001:100\npending=no\nappsync=\n'
    return (fixtures.fixture() + '\n@@SETUP_META\n' + metadata +
            '\n@@SETUP_END\n11111111-1111-4111-8111-111111111111\n')


def state():
    value = setup.parse_readback(wire())
    value['guard']['disabled'] = False
    for k, kind in setup.UPDATES.items():
        value['settings'][k] = {'type': kind, 'value': True if kind == 'Boolean' else 1}
    value['packages']['adplatform'] = '1.0'
    return value


def profile():
    data = setup.load_profile(setup.DEFAULT_PROFILE)
    data['packages'] = [r for r in data['packages'] if r['id'] == 'adplatform']
    return data


class FakeTV:
    def __init__(self, initial, report):
        self.state, self.report = copy.deepcopy(initial), report
        self.calls, self.locked = [], False
        self.fail = self.drift = None

    def snapshot(self):
        return copy.deepcopy(self.state)

    def run(self, command, label, timeout=30):
        # Parse but NEVER execute any generated shell command.
        parsed = subprocess.run(['bash', '-n'], input=command, text=True, capture_output=True)
        if parsed.returncode:
            raise AssertionError(parsed.stderr)
        self.calls.append((label, command))
        if label == self.fail:
            raise RuntimeError('simulated ambiguous failure')
        if label == 'backup-lock':
            self.locked = True
        elif label == 'native-guard':
            disabled = self.state['guard']['disabled']
            return ('uid=5001 appinfo rc=' + ('-3\ndisabled appinfo rc=0' if disabled else '0') +
                    '\nexec rc=0 value=' + setup.GUARD_EXEC + '\nsupport rc=0 value=0\ndisabled rc=0 value=' + str(int(disabled)))
        elif label == 'updates':
            for key in setup.UPDATES:
                self.state['settings'][key]['value'] = False if setup.UPDATES[key] == 'Boolean' else 0
        elif label == 'guard':
            self.state['guard']['disabled'] = True
            return 'caller uid=5001\naul_reload_appinfo rc=0'
        elif label == 'remove':
            package = command.splitlines()[-1].split()[-1]
            del self.state['packages'][package]
        elif label == 'unlock':
            self.locked = False
        if label == self.drift:
            self.state['packages']['unexpected.app'] = '1'
        return ''


class ProfileTests(unittest.TestCase):
    def test_conservative_profile_keeps_personal_apps_and_shared_components(self):
        data = setup.load_profile(setup.DEFAULT_PROFILE)
        rows = {r['id']: r for r in data['packages']}
        self.assertEqual(set(rows), set(setup.check.catalog_ids()))
        self.assertEqual(sum(r['remove'] for r in rows.values()), 24)
        self.assertFalse(rows['rJeHak5zRg']['remove'])
        self.assertFalse(rows['9Ur5IzDKqV']['remove'])
        self.assertFalse(rows['J9fZJDMi1v']['remove'])
        self.assertFalse(rows['com.samsung.tv.airplay-app-2']['remove'])
        self.assertFalse(set(rows) & set(setup.check.PROTECTED))

    def test_bad_profiles_rejected_before_network(self):
        original = profile()
        variants = [[], {'schema': True, 'name': 'x', 'packages': []}]
        for value in ['org.tizen.netflix-app', 'org.tizen.was-appsync', 'new.unknown', 'x; reboot', ['x']]:
            d = copy.deepcopy(original); d['packages'][0]['id'] = value; variants.append(d)
        for value in [1, 'true', None]:
            d = copy.deepcopy(original); d['packages'][0]['remove'] = value; variants.append(d)
        d = copy.deepcopy(original); d['packages'] *= 2; variants.append(d)
        d = copy.deepcopy(original); d['packages'][0]['command'] = 'anything'; variants.append(d)
        d = copy.deepcopy(original); d['packages'][0]['name'] = '\x1b[31mhidden'; variants.append(d)
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / 'profile.json'
            for d in variants:
                p.write_text(json.dumps(d))
                with self.subTest(data=d), self.assertRaises(ValueError):setup.load_profile(p)

    def test_duplicate_json_fields_are_not_silently_overridden(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / 'profile.json'
            p.write_text('{"schema":1,"schema":1,"name":"ambiguous","packages":[]}')
            with self.assertRaises(ValueError):setup.load_profile(p)

    def test_empty_selection_is_policies_only_in_effect(self):
        p = profile(); p['packages'] = []
        plan = setup.make_plan(state(), p)
        self.assertFalse(plan['guard_needed'])
        self.assertEqual({a['kind'] for a in plan['actions']}, {'updates'})


class ReadbackTests(unittest.TestCase):
    def test_strict_readback(self):
        s = setup.parse_readback(wire())
        setup.preflight(s)
        self.assertEqual(s['setup']['owner'], '5001:100')

    def test_malformed_metadata_and_schema_fail(self):
        for text in [wire().replace('@@SETUP_END', '@@OTHER'),
                     wire() + '\nextra', wire().replace('profile=9', 'model=9'),
                     wire().replace('pending=no', 'pending=no\npending=no')]:
            with self.subTest(text=text[-80:]), self.assertRaises(ValueError):setup.parse_readback(text)

    def test_unknown_build_identity_pending_update_and_running_sync_fail(self):
        for field, value in [('arch', 'aarch64'), ('model', '999'), ('profile', '8'),
                             ('owner', '5002:100'), ('uid', '5001'), ('pending', 'yes'), ('appsync', '1234')]:
            s = state(); s['setup'][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):setup.preflight(s)
        s = state(); s['firmware_build'] = 'different'
        with self.assertRaises(ValueError):setup.preflight(s)
        for key in setup.PENDING:
            s = state(); s['settings'][key]['value'] = 1
            with self.assertRaises(ValueError):setup.preflight(s)

    def test_foreign_guard_and_typed_update_values_fail(self):
        for key, value in [('exec', '/foreign'), ('support_disable', True), ('uid_override_rows', 1)]:
            s = state(); s['guard'][key] = value
            with self.assertRaises(ValueError):setup.preflight(s)
        for value in [2, -1, '0', False]:
            s = state(); s['settings'][next(iter(setup.UPDATES))]['value'] = value
            with self.assertRaises(ValueError):setup.preflight(s)

    def test_readonly_boundary(self):
        for forbidden in ('pkgcmd -u', 'vconftool set', 'systemctl', 'owner-reload', 'owner-launch',
                          'notification_post', 'input_keyevent', 'UPDATE ', 'DELETE ', 'iptables'):
            self.assertNotIn(forbidden, setup.READ_COMMAND)
        self.assertNotIn('noti_list', setup.READ_COMMAND)


class PlanTests(unittest.TestCase):
    def test_order_updates_guard_removal(self):
        plan = setup.make_plan(state(), profile())
        self.assertEqual([a['kind'] for a in plan['actions']], ['updates', 'guard', 'remove'])
        self.assertEqual(len(plan['actions'][0]['keys']), 4)

    def test_policies_only_cannot_remove_or_enable_guard(self):
        plan = setup.make_plan(state(), profile(), policies_only=True)
        self.assertFalse(plan['selected'])
        self.assertFalse(plan['guard_needed'])
        self.assertEqual(len(plan['actions']), 1)

    def test_idempotent_configured_state_has_no_actions(self):
        s = setup.parse_readback(wire())
        self.assertEqual(setup.make_plan(s, profile())['actions'], [])


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.tv = FakeTV(state(), Path(self.tmp.name))

    def execute(self):
        with contextlib.redirect_stdout(io.StringIO()):
            setup.apply(self.tv, self.tv.snapshot(), setup.make_plan(self.tv.snapshot(), profile()), {})

    def test_apply_checks_every_step_and_never_launches_or_reboots(self):
        self.execute()
        self.assertNotIn('adplatform', self.tv.state['packages'])
        self.assertIn('com.samsung.tv.cobalt', self.tv.state['packages'])
        self.assertTrue(self.tv.state['guard']['disabled'])
        self.assertFalse(self.tv.locked)
        self.assertTrue((self.tv.report / 'completed.json').exists())
        commands = '\n'.join(command for label, command in self.tv.calls)
        self.assertNotIn('owner-launch', commands)
        self.assertNotIn('systemctl reboot', commands)
        self.assertNotIn('scrsaver timeout', commands)
        self.assertNotIn('iptables', commands)
        self.assertEqual(sum(label == 'native-guard' for label, cmd in self.tv.calls), 3)

    def test_partial_failure_stops_with_lock_and_no_replay(self):
        self.tv.fail = 'remove'
        with self.assertRaises(RuntimeError):self.execute()
        self.assertTrue(self.tv.locked)
        self.assertEqual(sum(label == 'remove' for label, cmd in self.tv.calls), 1)
        self.assertNotIn('unlock', [label for label, cmd in self.tv.calls])
        self.assertFalse((self.tv.report / 'completed.json').exists())

    def test_unexpected_package_drift_aborts_further_changes(self):
        self.tv.drift = 'updates'
        with self.assertRaises(RuntimeError):self.execute()
        self.assertEqual(sum(label == 'updates' for label, cmd in self.tv.calls), 1)
        self.assertNotIn('remove', [label for label, cmd in self.tv.calls])
        self.assertTrue(self.tv.locked)

    def test_nonzero_owner_reload_status_stops_before_removal(self):
        original = self.tv.run
        def unexpected(command, label, timeout=30):
            output = original(command, label, timeout)
            return output.replace('aul_reload_appinfo rc=0', 'aul_reload_appinfo rc=1')
        with patch.object(self.tv, 'run', side_effect=unexpected), self.assertRaises(RuntimeError):
            self.execute()
        self.assertTrue(self.tv.locked)
        self.assertNotIn('remove', [label for label, _ in self.tv.calls])

    def test_confirmation_drift_stops_before_even_backups(self):
        original = self.tv.snapshot()
        self.tv.state['packages']['adplatform'] = 'changed'
        with self.assertRaises(RuntimeError):
            setup.apply(self.tv, original, setup.make_plan(original, profile()), {})
        self.assertEqual(self.tv.calls, [])

    def test_noop_does_not_stage_backup_refresh_or_set(self):
        s = setup.parse_readback(wire())
        with contextlib.redirect_stdout(io.StringIO()):
            setup.apply(self.tv, s, setup.make_plan(s, profile()), {})
        self.assertEqual(self.tv.calls, [])

    def test_existing_guard_is_read_not_reapplied(self):
        self.tv.state['guard']['disabled'] = True
        self.execute()
        commands = '\n'.join(c for _, c in self.tv.calls)
        self.assertNotIn('systemctl restart', commands)
        self.assertNotIn('owner-reload', commands)

    def test_unexpected_package_boot_setting_or_guard_changes_rejected(self):
        for variant in ('version', 'boot', 'setting', 'guard'):
            a, b = state(), state()
            if variant == 'version':b['packages']['com.samsung.tv.cobalt'] = 'other'
            elif variant == 'boot':b['boot_id'] = '22222222-2222-4222-8222-222222222222'
            elif variant == 'setting':b['settings'][next(iter(setup.UPDATES))]['value'] = 0
            else:b['guard']['disabled'] = True
            with self.assertRaises(RuntimeError):setup.stable(a, b)


class CommandTests(unittest.TestCase):
    def test_no_arguments_show_help_without_contacting_tv(self):
        with patch.object(setup, 'TV') as tv, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(setup.main([]), 0)
            tv.assert_not_called()

    def test_transport_failure_is_not_replayed(self):
        with tempfile.TemporaryDirectory() as td:
            tv = setup.TV('192.0.2.10', Path(td))
            failure = subprocess.CompletedProcess([], 125, '', 'unreachable')
            with patch.object(setup.subprocess, 'run', return_value=failure) as run:
                with self.assertRaises(RuntimeError):tv.run('true', 'test')
                self.assertEqual(run.call_count, 1)
            self.assertEqual(len(list(Path(td).glob('*.json'))), 2)
            for path in Path(td).glob('*.json'):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_stdout_cap_warning_is_failure_even_with_exit_zero(self):
        with tempfile.TemporaryDirectory() as td, patch.object(setup.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '', 'tv-access: output capped at 1 MiB per stream')):
            with self.assertRaises(RuntimeError):setup.TV('192.0.2.10', Path(td)).run('true', 'test')

    def test_private_json_never_overwrites(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / 'data.json'; setup.private_json(p, {'a': 1})
            with self.assertRaises(FileExistsError):setup.private_json(p, {'a': 2})
            self.assertEqual(json.loads(p.read_text()), {'a': 1})

    def test_plan_verify_and_unconfirmed_apply_never_call_mutations(self):
        for operation in ('plan', 'verify', 'apply'):
            with tempfile.TemporaryDirectory() as td, patch.object(setup.TV, 'snapshot', return_value=state()), patch.object(setup, 'apply') as apply, patch.object(setup.sys.stdin, 'isatty', return_value=False), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                rc = setup.main([operation, '--tv', '192.0.2.10', '--reports', td])
                self.assertEqual(rc, 0 if operation == 'plan' else 1)
                apply.assert_not_called()

    def test_helper_hashes_required(self):
        with tempfile.TemporaryDirectory() as td, patch.object(setup, 'ROOT', Path(td)):
            d = Path(td) / 'tools/app_sync_guard'; d.mkdir(parents=True)
            (d / 'SHA256SUMS').write_text('0' * 64 + '  PkgInfoProbe.cs\n')
            with self.assertRaises(ValueError):setup.helper_files(True)


if __name__ == '__main__':
    unittest.main()
