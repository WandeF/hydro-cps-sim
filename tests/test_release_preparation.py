"""Offline acceptance tests: preserve scenarios and exported data contracts."""
import csv
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from src.run.prepare_config import PATH_KEYS, prepare_config
from src.core.config import load_yaml, load_runtime_config
from src.control.st_generation import generate_and_validate
from src.network.ns3_generation import generate_cc
from src.attack.launch import (_scenario_list, _iter_mitm_targets, _iter_dos_targets,
                               _iter_openplc_targets)
from src.io.export_results import export_attack_events, export_actuator_state

ROOT = Path(__file__).resolve().parents[1]


class ReleasePreparationTests(unittest.TestCase):
    def dependencies(self, root):
        for name in ('OpenPLC_v3/webserver/scripts/compile_program.sh', 'ns-3-dev-git/ns3'):
            p = root/name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.touch()

    def test_all_scenarios_preserve_parameters_and_generate(self):
        for source in sorted((ROOT/'examples/c_town').glob('*.yaml')):
            with self.subTest(config=source.name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.dependencies(root)
                before = source.read_bytes()
                target = prepare_config(source, root/'local.yaml', root, root/'result')
                old, cfg = load_yaml(source), load_yaml(target)
                self.assertEqual({k:v for k,v in old.items() if k not in PATH_KEYS},
                                 {k:v for k,v in cfg.items() if k not in PATH_KEYS})
                self.assertEqual(before, source.read_bytes())
                self.assertTrue(generate_and_validate(target)['ok'])
                rt = load_runtime_config(target)
                self.assertEqual(len(rt.plcs), 8)
                self.assertRegex(generate_cc(cfg), r'int\s+main\s*\(')
                scenarios = _scenario_list(cfg)
                resolved = (list(_iter_mitm_targets(cfg, rt, root/'result/runtime')) +
                            list(_iter_dos_targets(cfg, rt)) +
                            list(_iter_openplc_targets(cfg, rt, root/'result/runtime')))
                self.assertGreaterEqual(len(resolved), len(scenarios))

    def test_existing_results_and_config_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.dependencies(root)
            source = ROOT/'examples/c_town/config.yaml'
            output = root/'result'
            output.mkdir()
            sentinel = output/'keep.csv'
            sentinel.write_text('unchanged')
            with self.assertRaisesRegex(ValueError, 'existing results'):
                prepare_config(source, root/'local.yaml', root, output)
            self.assertFalse((root/'local.yaml').exists())
            self.assertEqual(sentinel.read_text(), 'unchanged')
            destination = root/'local.yaml'
            destination.write_text('keep')
            with self.assertRaisesRegex(ValueError, 'overwrite'):
                prepare_config(source, destination, root, root/'new-result')
            self.assertEqual(destination.read_text(), 'keep')

    def test_missing_dependency_is_reported_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaisesRegex(ValueError, 'OpenPLC compiler not found'):
                prepare_config(ROOT/'examples/c_town/config.yaml', root/'copy.yaml', root, root/'out')
            self.assertFalse((root/'copy.yaml').exists())
            self.assertFalse((root/'out').exists())

    def test_cli_help_version_and_missing_arguments(self):
        for args, expected in ((['--help'], 0), (['--version'], 0), ([], 2)):
            result = subprocess.run([sys.executable, '-m', 'src.run.prepare_config', *args],
                                    cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(result.returncode, expected, result.stderr)
        result = subprocess.run(['bash', 'scripts/run_all.sh', '--help'], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn('--logic-wait', result.stdout)

    def test_export_preserves_values_and_empty_event_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'raw').mkdir()
            (root/'raw/actuator_state.jsonl').write_text(json.dumps({'iteration': 2, 'PU1': True})+'\n')
            path = export_actuator_state(root, root/'reports')
            with path.open() as f:
                reader = csv.DictReader(f)
                self.assertEqual(reader.fieldnames, ['iteration', 'PU1'])
                self.assertEqual(next(reader), {'iteration': '2', 'PU1': 'True'})
            path = export_attack_events(root, root/'reports')
            with path.open() as f:
                reader = csv.DictReader(f)
                self.assertIn('old_value', reader.fieldnames)
                self.assertIn('new_value', reader.fieldnames)
                self.assertEqual(list(reader), [])


if __name__ == '__main__':
    unittest.main()
