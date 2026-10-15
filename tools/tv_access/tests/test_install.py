"""Local installation tests in isolated temporary homes; no TV connection."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.bindir = self.home/'bin'
        self.skilldir = self.home/'skill'
        self.config = self.home/'config.json'
        self.env = {'HOME': str(self.home), 'PATH': os.environ.get('PATH', ''),
                    'TV_ACCESS_CONFIG': str(self.config)}

    def install(self, *args):
        return subprocess.run([sys.executable, str(ROOT/'install.py'), '--bin-dir',
                               str(self.bindir), *args], env=self.env,
                              capture_output=True, text=True, timeout=15)

    def test_config_and_links(self):
        result = self.install('--tv', 'tv.example.invalid', '--skill-dir', str(self.skilldir))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.config.read_text()), {'tv_host': 'tv.example.invalid'})
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)
        for name in ('tv-access', 'tv-key', 'tv-shot'):
            self.assertEqual((self.bindir/name).resolve(), ROOT/name)
        self.assertEqual((self.skilldir/'SKILL.md').resolve(), ROOT/'SKILL.md')

    def test_repeated_install_is_idempotent(self):
        self.assertEqual(self.install().returncode, 0)
        self.assertEqual(self.install().returncode, 0)
        self.assertFalse(self.config.exists())

    def test_conflict_preflight_preserves_every_destination(self):
        self.bindir.mkdir()
        old = self.bindir/'tv-shot'
        old.write_text('keep original')
        result = self.install('--tv', 'tv.example.invalid')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(old.read_text(), 'keep original')
        self.assertFalse((self.bindir/'tv-access').exists())
        self.assertFalse(self.config.exists())

    def test_force_replaces_a_conflicting_file(self):
        self.bindir.mkdir()
        old = self.bindir/'tv-access'
        old.write_text('old wrapper')
        self.assertEqual(self.install('--force').returncode, 0)
        self.assertEqual(old.resolve(), ROOT/'tv-access')

    def test_force_refuses_directory(self):
        (self.bindir/'tv-access').mkdir(parents=True)
        self.assertNotEqual(self.install('--force').returncode, 0)
        self.assertTrue((self.bindir/'tv-access').is_dir())

    def test_invalid_host_no_install(self):
        self.assertNotEqual(self.install('--tv', 'bad host').returncode, 0)
        self.assertFalse(self.config.exists())
        self.assertFalse(self.bindir.exists())

    def test_invalid_config_not_overwritten(self):
        self.config.write_text('private original, not JSON')
        self.assertNotEqual(self.install('--tv', 'tv.example.invalid').returncode, 0)
        self.assertEqual(self.config.read_text(), 'private original, not JSON')
        self.assertFalse(self.bindir.exists())


if __name__ == '__main__':
    unittest.main()
