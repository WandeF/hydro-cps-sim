"""Installer preflight must be safe before privileged installation starts."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'install.sh'


class InstallPreflightTests(unittest.TestCase):
    def invoke(self, *args, **kwargs):
        return subprocess.run(['bash', str(SCRIPT), *args], text=True,
                              capture_output=True, **kwargs)

    def test_dry_run_handles_spaces_without_creating_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp) / 'dependencies with spaces'
            result = self.invoke('--parent', str(parent), '--dry-run')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(str(parent), result.stdout)
            self.assertFalse(parent.exists())

    def test_invalid_arguments_fail_before_installation(self):
        for args in [('--jobs', '0'), ('--jobs', '-2'), ('--env', 'base'),
                     ('--env', '../bad'), ('--parent',), ('--unknown',)]:
            with self.subTest(args=args):
                result = self.invoke(*args)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('[ERROR]', result.stderr)

    def test_existing_non_repository_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / 'DHALSIM-epynet'
            repo.mkdir()
            sentinel = repo / 'keep.txt'
            sentinel.write_text('keep')
            env = dict(os.environ, CONDA_EXE='/usr/bin/true')
            result = self.invoke('--parent', tmp, env=env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('not a Git checkout', result.stderr)
            self.assertEqual(sentinel.read_text(), 'keep')


    def test_unexpected_repository_origin_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / 'DHALSIM-epynet'
            subprocess.run(['git', 'init', '-q', str(repo)], check=True)
            subprocess.run(['git', '-C', str(repo), 'remote', 'add', 'origin',
                            'https://example.invalid/unrelated.git'], check=True)
            env = dict(os.environ, CONDA_EXE='/usr/bin/true')
            result = self.invoke('--parent', tmp, env=env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Unexpected repository origin', result.stderr)


    def test_clone_is_published_only_after_success(self):
        script = SCRIPT.read_text()
        function = script.split('clone_repo() {', 1)[1].split('\nclone_repo "$EPYNET"', 1)[0]
        function = 'clone_repo() {' + function
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            source = parent / 'source'
            subprocess.run(['git', 'init', '-q', str(source)], check=True)
            (source / 'example.txt').write_text('source content')
            subprocess.run(['git', '-C', str(source), 'add', '.'], check=True)
            subprocess.run(['git', '-C', str(source), '-c', 'user.name=Test',
                            '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'test'], check=True)
            rev = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
            command = 'set -euo pipefail\nPARENT_DIR="$1"\n' + function + '\nclone_repo "$2" "$3" "$4"'
            for revision, success in [('0' * 40, False), (rev, True)]:
                target = parent / 'dependencies with spaces'
                result = subprocess.run(['bash', '-c', command, '_', str(parent),
                                         str(target), str(source), revision], capture_output=True, text=True)
                self.assertEqual(result.returncode == 0, success, result.stderr)
                self.assertEqual(target.exists(), success)
                self.assertEqual(list(parent.glob('.hydro-clone.*')), [])
            self.assertEqual((target / 'example.txt').read_text(), 'source content')


if __name__ == '__main__':
    unittest.main()
