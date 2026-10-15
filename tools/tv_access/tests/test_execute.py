"""Orchestration regression tests, using a fake SDB transport (never a TV)."""
import contextlib
import io
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tv_sdb
from sdb_direct import SDBError


class ExecuteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        for rel in ('build/SdbJob.dll', 'bootstrap/SamsungTvArchiveRoot.dll',
                    'bootstrap/SamsungTvArchiveRoot.runtimeconfig.json'):
            path = root/rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'offline-fixture')
        self.fixture_root = root
        self.sdk_commands, self.uploads, self.writes, self.launches = [], [], [], []
        self.attach_failures = 0
        self.lost_ack = False
        self.set_result()
        self.patch('tv_sdb.HERE', root)
        self.patch('tv_sdb.DirectSDB', self.connection)
        self.patch('tv_sdb.build_package', self.build)
        self.patch('tv_sdb.time.sleep', lambda _: None)

    def patch(self, target, replacement):
        context = patch(target, replacement)
        context.start()
        self.addCleanup(context.stop)

    def set_result(self, uid=0, code=7, stdout=b'stdout', stderr=b'stderr', magic=b'TSR1', flags=0):
        self.result = struct.pack('<4siIIII', magic, code, len(stdout), len(stderr), flags, uid)+stdout+stderr

    def build(self, work, launch):
        self.launches.append(launch)
        files = []
        for name in ('public.pem', 'passwd', 'archive', 'signature'):
            path = Path(work)/name
            path.write_bytes(b'offline-fixture')
            files.append(path)
        return files

    def connection(self, host, timeout=15):
        self.assertEqual(host, 'offline.invalid')
        test = self

        class Connection:
            def __enter__(self):
                self.reply = bytearray(test.result)
                return self
            def __exit__(self, *_):
                pass
            def execute_sdk(self, command):
                test.sdk_commands.append(command)
            def push(self, data, path, mode):
                test.uploads.append((path, mode))
            def open(self, service):
                test.assertTrue(service.startswith('tcp:'))
                if test.attach_failures:
                    test.attach_failures -= 1
                    raise SDBError('offline simulated attach refusal')
            def write(self, data):
                # Record only sizes or ACK, never the per-job key itself.
                test.writes.append(data if data == b'ACK1' else len(data))
                if test.lost_ack and data == b'ACK1':
                    raise SDBError('offline simulated lost ACK')
            def read(self, n):
                chunk = bytes(self.reply[:n])
                del self.reply[:n]
                return chunk
        return Connection()

    def bootstrap_count(self):
        return sum('SamsungTvArchiveRoot.dll' in command for command in self.sdk_commands)

    def test_one_bootstrap_guard_and_streams(self):
        self.assertEqual(tv_sdb.execute('offline.invalid', 'printf test', 15),
                         (7, b'stdout', b'stderr', 0))
        self.assertEqual(self.bootstrap_count(), 1)
        self.assertEqual(self.writes, [32, b'ACK1'])
        self.assertEqual(len(self.uploads), 10)
        self.assertIn(b'mkdir "$D/run.once"', self.launches[0])
        self.assertTrue(all('/archive-root-' in path for path, _ in self.uploads))

    def test_attach_retry_does_not_replay(self):
        self.attach_failures = 2
        tv_sdb.execute('offline.invalid', 'printf test', 15)
        self.assertEqual(self.bootstrap_count(), 1)
        self.assertEqual(self.attach_failures, 0)

    def test_lost_ack_retains_verified_result(self):
        self.lost_ack = True
        with contextlib.redirect_stderr(io.StringIO()) as diagnostic:
            result = tv_sdb.execute('offline.invalid', 'printf test', 15)
        self.assertEqual(result[:3], (7, b'stdout', b'stderr'))
        self.assertIn('ACK uncertain', diagnostic.getvalue())
        self.assertEqual(self.bootstrap_count(), 1)

    def test_nonroot_rejected_without_replay(self):
        self.set_result(uid=901)
        with self.assertRaises(SDBError):
            tv_sdb.execute('offline.invalid', 'printf test', 15)
        self.assertEqual(self.bootstrap_count(), 1)
        self.assertNotIn(b'ACK1', self.writes)

    def test_invalid_magic_rejected_without_replay(self):
        self.set_result(magic=b'BAD1')
        with self.assertRaises(SDBError):
            tv_sdb.execute('offline.invalid', 'printf test', 15)
        self.assertEqual(self.bootstrap_count(), 1)

    def test_flags_and_binary_output_preserved(self):
        self.set_result(code=124, stdout=b'\x00\xff', stderr=b'\xfe', flags=15)
        self.assertEqual(tv_sdb.execute('offline.invalid', 'printf test', 1),
                         (124, b'\x00\xff', b'\xfe', 15))

    def test_invalid_commands_never_connect(self):
        for options in (['--timeout', '0', 'true'], ['--timeout', '301', 'true'],
                        ['a'*65537], ['bad\x00command']):
            with self.subTest(options=options[:2]):
                with patch('sys.argv', ['tv-access', *options]), \
                     patch('tv_sdb.DirectSDB') as connect, \
                     contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        tv_sdb.main()
                    connect.assert_not_called()


if __name__ == '__main__':
    unittest.main()
