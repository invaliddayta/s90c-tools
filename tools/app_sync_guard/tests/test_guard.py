"""Offline only: synthetic readbacks and disposable SQLite databases. No TV calls."""
import ast
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('guard_check', ROOT / 'check.py')
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)


def fixture():
    values = []
    for key in check.KEYS:
        if check.UPDATE_TYPES.get(key) == 'Boolean':
            value, kind = 'False', 'Boolean'
        else:
            value, kind = '0', 'Int32'
        values.append(f'{key}, value = {value} ({kind})')
    sections = {
        'BOOT': '11111111-1111-4111-8111-111111111111\n9000.1 32000.0\nbtime 1000000000',
        'PACKAGES': 'system apps\tpkg_type [rpm]\tpkgid [com.samsung.tv.cobalt]\tname [Cobalt]\tversion [23.2.309]\tstorage [internal]\ncommand elapsed: 40ms',
        'GUARD': 'org.tizen.was-appsync|true|false|/usr/apps/org.tizen.was-appsync/bin/was-appsync\n0',
        'FIRMWARE': 'a' * 64 + '  /etc/os-release\nBUILD_ID=T-PTMCDEUC-REL-202408210126',
        'SETTINGS': '\n'.join(values),
        'BOOT_END': '11111111-1111-4111-8111-111111111111',
        'END': '',
    }
    return ''.join(f'\n@@{key}\n{value}\n' for key, value in sections.items())


class SnapshotTests(unittest.TestCase):
    def test_complete_typed_readback(self):
        got = check.parse_snapshot(fixture())
        self.assertTrue(got['guard']['disabled'])
        self.assertFalse(got['guard']['support_disable'])
        self.assertEqual(got['guard']['uid_override_rows'], 0)
        self.assertEqual(got['settings']['db/apps/auto_update'], {'type': 'Boolean', 'value': False})
        self.assertEqual(got['package_count'], 1)
        self.assertEqual(got['catalog_present'], [])

    def test_compare_reports_real_delta_not_count_only(self):
        a = check.parse_snapshot(fixture())
        b = copy.deepcopy(a)
        b['boot_id'] = '22222222-2222-4222-8222-222222222222'
        b['packages']['rJeHak5zRg'] = '2.1.71'
        b['packages']['com.samsung.tv.cobalt'] = 'different'
        delta = check.compare(a, b)
        self.assertTrue(delta['boot_changed'])
        self.assertEqual(delta['packages_added'], ['rJeHak5zRg'])
        self.assertEqual(delta['versions_changed']['com.samsung.tv.cobalt'], ['23.2.309', 'different'])
        self.assertFalse(delta['os_release_changed'])

    def test_removed_package_and_typed_setting_delta(self):
        a = check.parse_snapshot(fixture())
        b = copy.deepcopy(a)
        b['packages'] = {}
        b['settings']['db/apps/auto_update'] = {'type': 'Int32', 'value': 0}
        delta = check.compare(a, b)
        self.assertEqual(delta['packages_removed'], ['com.samsung.tv.cobalt'])
        self.assertIn('db/apps/auto_update', delta['settings_changed'])

    def test_malformed_readbacks_rejected(self):
        src = fixture()
        bad = [
            src.replace('@@END', '@@MISSING'),
            src.replace('@@GUARD', '@@BOGUS'),
            src.replace('9000.1 32000.0', 'nan 32000.0'),
            src.replace('11111111-1111-4111-8111-111111111111', ''),
            src.replace('@@BOOT_END\n11111111', '@@BOOT_END\n22222222'),
            src.replace('(Int32)', '(String)', 1),
            src.replace('org.tizen.was-appsync|true', 'wrong.app|true'),
            src.replace('org.tizen.was-appsync|true', 'org.tizen.was-appsync|1'),
            src.replace('value = False (Boolean)', 'value = bogus (Boolean)'),
            src.replace('db/apps/auto_update, value', 'missing, value'),
            src.replace('BUILD_ID=', 'MISSING='),
            src.replace('a' * 64, 'brokenhash'),
            src + '\n@@END\n',
        ]
        for output in bad:
            with self.subTest(output=output[-70:]):
                with self.assertRaises((ValueError, IndexError)):
                    check.parse_snapshot(output)

    def test_no_network_without_explicit_live(self):
        with patch.object(check.subprocess, 'run') as run, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                check.main(['--output', 'unused.json'])
            run.assert_not_called()

    def test_output_never_overwritten(self):
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / 'existing.json'
            dest.write_text('keep')
            with patch.object(check.subprocess, 'run') as run, contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    check.main(['--live', '--output', str(dest)])
                run.assert_not_called()
            self.assertEqual(dest.read_text(), 'keep')

    def test_collect_saves_private_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / 'state.json'
            response = subprocess.CompletedProcess([], 0, fixture(), '')
            with patch.object(check.subprocess, 'run', return_value=response) as run, contextlib.redirect_stdout(io.StringIO()):
                rc = check.main(['--live', '--tv', '192.0.2.10', '--output', str(dest)])
            self.assertEqual(rc, 0)
            self.assertEqual(stat.S_IMODE(dest.stat().st_mode), 0o600)
            self.assertEqual(json.loads(dest.read_text())['schema'], 2)
            self.assertIn('192.0.2.10', run.call_args.args[0])
            self.assertEqual(run.call_count, 1)

    def test_failed_transport_never_retried_or_saved(self):
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / 'failed.json'
            response = subprocess.CompletedProcess([], 125, '', 'test failure')
            with patch.object(check.subprocess, 'run', return_value=response) as run, contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(check.main(['--live', '--output', str(dest)]), 1)
            self.assertEqual(run.call_count, 1)
            self.assertFalse(dest.exists())

    def test_catalog_excludes_protected_packages(self):
        ids = check.catalog_ids()
        self.assertEqual(len(ids), 46)
        self.assertIn('9Ur5IzDKqV', ids)
        self.assertIn('rJeHak5zRg', ids)
        self.assertNotIn('org.tizen.pvod-appfw', ids)
        self.assertFalse(set(ids).intersection(check.PROTECTED))

    def test_readonly_command_boundary(self):
        for forbidden in ('systemctl', 'app_launcher', 'owner-reload', 'UPDATE ',
                          'DELETE ', 'INSERT ', 'vconftool set', 'pkgcmd -u', 'input_keyevent',
                          'dump_screen', 'scrsaver timeout', 'iptables'):
            with self.subTest(command=forbidden):
                self.assertNotIn(forbidden, check.COMMAND)
        self.assertNotRegex(check.COMMAND, r'(?m)^\s*(reboot|poweroff|shutdown)(?:\s|$)')


class SQLTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / 'test.db'
        with contextlib.closing(sqlite3.connect(self.db)) as c, c:
            c.executescript('CREATE TABLE package_app_info(app_id TEXT, app_disable TEXT, app_support_disable TEXT, app_exec TEXT); CREATE TABLE package_app_info_for_uid(app_id TEXT, uid INTEGER);')
            c.execute('INSERT INTO package_app_info VALUES(?,?,?,?)', (check.APP_ID, 'false', 'false', '/usr/apps/org.tizen.was-appsync/bin/was-appsync'))
            c.execute('INSERT INTO package_app_info VALUES(?,?,?,?)', ('other.app', 'false', 'true', '/untouched'))

    def tearDown(self):
        self.tmp.cleanup()

    def execute(self, name):
        c = sqlite3.connect(self.db)
        try:
            c.executescript((ROOT / name).read_text())
        finally:
            # Like sqlite3 -bail exiting, close rolls back an uncommitted transaction.
            c.close()

    def rows(self):
        with contextlib.closing(sqlite3.connect(self.db)) as c, c:
            return c.execute('SELECT * FROM package_app_info ORDER BY app_id').fetchall()

    def test_apply_and_rollback_only_one_field(self):
        before = self.rows()
        self.execute('apply.sql')
        changed = self.rows()
        self.assertEqual(changed[0][1], 'true')
        self.assertEqual(changed[0][0::2], before[0][0::2])
        self.assertEqual(changed[0][3], before[0][3])
        self.assertEqual(changed[1], before[1])
        self.execute('rollback.sql')
        self.assertEqual(self.rows(), before)

    def test_reapply_fails_without_changing_state(self):
        self.execute('apply.sql')
        before = self.rows()
        with self.assertRaises(sqlite3.IntegrityError):
            self.execute('apply.sql')
        self.assertEqual(self.rows(), before)

    def test_rollback_unapplied_fails(self):
        before = self.rows()
        with self.assertRaises(sqlite3.IntegrityError):
            self.execute('rollback.sql')
        self.assertEqual(self.rows(), before)

    def test_foreign_override_blocks_apply(self):
        with contextlib.closing(sqlite3.connect(self.db)) as c, c:
            c.execute('INSERT INTO package_app_info_for_uid VALUES(?,5001)', (check.APP_ID,))
        before = self.rows()
        with self.assertRaises(sqlite3.IntegrityError):
            self.execute('apply.sql')
        self.assertEqual(self.rows(), before)

    def test_wrong_exec_or_support_flag_blocks_apply(self):
        for column, value in [('app_exec', '/changed'), ('app_support_disable', 'true')]:
            with self.subTest(column=column):
                with contextlib.closing(sqlite3.connect(self.db)) as c, c:
                    c.execute(f'UPDATE package_app_info SET {column}=? WHERE app_id=?', (value, check.APP_ID))
                before = self.rows()
                with self.assertRaises(sqlite3.IntegrityError):
                    self.execute('apply.sql')
                self.assertEqual(self.rows(), before)

    def test_duplicate_app_rows_roll_back_all_changes(self):
        with contextlib.closing(sqlite3.connect(self.db)) as c, c:
            c.execute('INSERT INTO package_app_info SELECT * FROM package_app_info WHERE app_id=?', (check.APP_ID,))
        before = self.rows()
        with self.assertRaises(sqlite3.IntegrityError):
            self.execute('apply.sql')
        self.assertEqual(self.rows(), before)


class DocumentationTests(unittest.TestCase):
    REPO = ROOT.parents[1]
    DOCUMENTS = ('README.md', 'SECURITY.md', 'THIRD_PARTY.md',
                 'docs/usage.md', 'docs/debloat.md', 'tools/tv_access/SKILL.md')

    def test_local_markdown_links_resolve(self):
        for relative in self.DOCUMENTS:
            path = self.REPO / relative
            for link in re.findall(r'\]\(([^)]+)\)', path.read_text()):
                if '://' in link or link.startswith(('mailto:', '#')):
                    continue
                target = unquote(link.split('#', 1)[0])
                with self.subTest(file=relative, target=target):
                    self.assertTrue((path.parent / target).exists())

    def test_shell_examples_parse_without_execution(self):
        for relative in self.DOCUMENTS:
            for block in re.findall(r'```sh\n(.*?)\n```', (self.REPO / relative).read_text(), re.S):
                with self.subTest(file=relative, first_line=block.splitlines()[0]):
                    result = subprocess.run(['bash', '-n'], input=block, text=True, capture_output=True)
                    self.assertEqual(result.returncode, 0, result.stderr)

    def test_python_heredocs_parse_without_execution(self):
        for relative in self.DOCUMENTS:
            pattern = r"python3 - <<'PY'\n(.*?)\nPY"
            for block in re.findall(pattern, (self.REPO / relative).read_text(), re.S):
                with self.subTest(file=relative):
                    ast.parse(block)

    def test_probe_source_matches_recorded_hash(self):
        entry = (ROOT / 'SHA256SUMS').read_text().splitlines()[0]
        digest, filename = entry.split()
        self.assertEqual(filename, 'PkgInfoProbe.cs')
        self.assertEqual(hashlib.sha256((ROOT / filename).read_bytes()).hexdigest(), digest)


if __name__ == '__main__':
    unittest.main()
