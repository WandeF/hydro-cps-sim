"""Interface, real solver, compatibility and protocol-component regression gates."""
import copy
import csv
import importlib.util
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

from src.core.config import load_runtime_config
from src.control.st_generation import generate_plc_st
from src.physics.base import PhysicsAdapter
from src.physics.config import physics_backend, physics_init_style, physics_timestep
from src.physics.factory import create_physics_adapter
from src.io.export_results import export_all, export_scada_observed
from src.runtime.persistent_closed_loop import persist_physics_snapshot
from src.attack.modbus_mitm import (AttackRule, AttackStateReader, ConnectionState,
    EventWriter, ModbusMitmProcessor)
from src.network.ns3_generation import generate_cc
from src.network.network_sh_generation import generate_network_sh

ROOT = Path(__file__).resolve().parents[1]
POWER = ROOT / 'examples/power_feeder/config.yaml'
HAVE_POWER = importlib.util.find_spec('pandapower') is not None
HAVE_WATER = importlib.util.find_spec('epynet') is not None


def prepare(tmp, source=POWER, mutate=None):
    raw = yaml.safe_load(source.read_text())
    raw['output_path'] = str(Path(tmp)/'output')
    if 'model_file' in raw.get('physics', {}):
        raw['physics']['model_file'] = str(source.parent/'model.yaml')
    if mutate:
        mutate(raw)
    path = Path(tmp)/'config.yaml'
    path.write_text(yaml.safe_dump(raw, sort_keys=False))
    st_dir = Path(raw['output_path'])/'st'
    st_dir.mkdir(parents=True, exist_ok=True)
    for plc in raw['plcs']:
        (st_dir/f"{plc['name'].lower()}.st").write_text(generate_plc_st(raw, plc))
    return load_runtime_config(path)


class SelectionTests(unittest.TestCase):
    def test_legacy_defaults_and_aliases(self):
        self.assertEqual(physics_backend({}), 'dhalsim_epynet')
        for alias in ('epynet', 'epanet', 'dhalsim_epynet'):
            self.assertEqual(physics_backend({'physics': {'backend': alias}}), 'dhalsim_epynet')
        self.assertEqual(physics_init_style({}), 'dhalsim')
        self.assertEqual(physics_init_style({'physics': {'backend': 'pandapower'}}), 'current')

    def test_invalid_selection_timestep_and_initialization(self):
        for raw in ({'physics': []}, {'physics': {'backend': 'mock'}},
                    {'physics': {'backend': 'not_installed'}}):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                physics_backend(raw)
        for value in (True, 0, -1, 'nan', 'inf', 'bad'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                physics_timestep({'physics': {'timestep': value}})
        with self.assertRaises(ValueError):
            physics_init_style({'physics': {'backend': 'pandapower', 'init_style': 'dhalsim'}})
        self.assertEqual(physics_timestep({'physics': {'backend': 'pandapower', 'timestep': 0.5}}), .5)
        with self.assertRaises(ValueError):
            physics_timestep({'physics': {'backend': 'epynet', 'timestep': .5}})

    def test_power_configuration_and_generators_need_no_epanet(self):
        with tempfile.TemporaryDirectory() as tmp:
            rt = prepare(tmp)
            self.assertNotIn('inp_file', rt.raw)
            self.assertEqual(rt.hydraulic_timestep, 1.0)
            self.assertEqual(len(rt.plcs), 2)
            self.assertIn('PLC1_V_CONTROL', rt.plcs['PLC2'].md_vars)
            self.assertIn('LOAD_SW', rt.plcs['PLC2'].coil_vars['PLC2_LOAD_SW'].tag)
            self.assertIn('ns-power-plc2', generate_network_sh(rt.raw))
            self.assertIn('TapBridgeHelper', generate_cc(rt.raw))
            result=subprocess.run([sys.executable,'-c',
                "import sys; from src.physics.factory import create_physics_adapter; "
                "from src.core.config import load_runtime_config; "
                "load_runtime_config(sys.argv[1]); assert 'epynet' not in sys.modules; assert 'pandapower' not in sys.modules",
                str(rt.config_path)],cwd=ROOT,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)

    def test_relative_output_paths_do_not_collapse_after_first_run(self):
        from src.run.config_info import _resolve_output_dir
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'output').mkdir()
            self.assertEqual(_resolve_output_dir(root/'cfg.yaml', 'output/mitm'), root/'output/mitm')

    def test_optional_dependency_failure_is_explicit(self):
        from src.physics.factory import check_physics_dependencies
        with patch('src.physics.factory.import_module', side_effect=ImportError('missing')):
            with self.assertRaisesRegex(ImportError, 'pandapower'):
                check_physics_dependencies('pandapower')

    def test_full_run_checker_rejects_missing_runs_and_accepts_bool_cells(self):
        from scripts.check_power_results import check, coil
        self.assertEqual(coil('True'),1)
        self.assertEqual(coil('False'),0)
        with self.assertRaises(ValueError): coil('2')
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError,'Missing full-run artifact'):
                check(Path(tmp))

    def test_nonwater_scada_key_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)/'runtime'
            (runtime/'raw').mkdir(parents=True)
            (runtime/'raw/scada_observed.jsonl').write_text(json.dumps({
                'iteration': 0,'plc':'PLC1','variable':'PLC1_V_CONTROL','value':1.01})+'\n')
            paths = export_scada_observed(runtime, Path(tmp)/'reports', key_variables=['PLC1.PLC1_V_CONTROL'])
            self.assertIn('PLC1.PLC1_V_CONTROL', paths[-1].read_text())


@unittest.skipUnless(HAVE_WATER, 'DHALSIM-epynet not installed')
class WaterRegressionTests(unittest.TestCase):
    def test_adapter_preserves_original_engine_snapshots_and_csv_schema(self):
        from src.physics.engine import PhysicsEngine
        from src.io.dhalsim import snapshot_to_dhalsim_row
        with tempfile.TemporaryDirectory() as tmp:
            rt = prepare(tmp, ROOT/'examples/c_town/config.yaml')
            direct = PhysicsEngine(rt, mode='dhalsim_epynet', work_dir=Path(tmp)/'direct')
            wrapped = create_physics_adapter(rt, work_dir=Path(tmp)/'wrapped')
            try:
                self.assertIsInstance(wrapped, PhysicsAdapter)
                self.assertEqual(direct.dhalsim_zero_snapshot(), wrapped.dhalsim_zero_snapshot())
                self.assertEqual(direct.dhalsim_initial_snapshot(), wrapped.dhalsim_initial_snapshot())
                self.assertEqual(direct.current_snapshot(), wrapped.initialize())
                for i in range(2,7):
                    state = dict(rt.actuator_initial_state)
                    if i >= 4:
                        state['PU1'] = True
                    left, right = direct.step(state, i), wrapped.step(state, i)
                    self.assertEqual(left, right)
                    a,b=snapshot_to_dhalsim_row(rt,left),snapshot_to_dhalsim_row(rt,right)
                    a.pop('timestamp'); b.pop('timestamp')
                    self.assertEqual(a,b)
            finally:
                direct.close(); wrapped.close(); wrapped.close()


@unittest.skipUnless(HAVE_POWER, 'Optional pandapower dependency not installed')
class PowerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.rt = prepare(self.tmp.name)
        self.physics = create_physics_adapter(self.rt)

    def tearDown(self):
        self.physics.close()
        self.tmp.cleanup()

    def test_real_initial_solution_and_idempotent_initialization(self):
        first = self.physics.initialize()
        self.assertEqual(first,self.physics.initialize())
        self.assertEqual(first['backend'],'pandapower')
        self.assertTrue(first['solver_converged'])
        self.assertAlmostEqual(first['values']['P_LOAD'],.8)
        self.assertGreater(first['values']['V_CONTROL'],.95)
        self.assertLess(first['values']['V_CONTROL'],1.02)
        state=self.physics.get_state(); state['V_CONTROL']=0
        self.assertGreater(self.physics.get_state()['V_CONTROL'],.95)

    def test_breaker_disconnects_load_and_changes_real_power(self):
        connected=self.physics.get_state()
        result=self.physics.step({'LOAD_SW':False},dt=.5)
        self.assertEqual(result['sim_time'],.5)
        self.assertEqual(result['values']['P_LOAD'],0)
        self.assertEqual(result['values']['V_LOAD'],0)
        self.assertEqual(result['unsupplied_buses'],[3])
        self.assertLess(result['values']['P_GRID'],connected['P_GRID'])
        restored=self.physics.step({'LOAD_SW':True})
        self.assertAlmostEqual(restored['values']['P_LOAD'],.8)
        self.assertEqual(restored['unsupplied_buses'],[])

    def test_invalid_commands_and_clock_do_not_advance(self):
        for commands in ({'UNKNOWN':True},{'LOAD_SW':1}):
            with self.assertRaises(ValueError): self.physics.step(commands)
        for dt in (0,-1,float('nan'),float('inf')):
            with self.assertRaises(ValueError): self.physics.step(dt=dt)
        self.assertEqual(self.physics.sim_time,0)
        with self.assertRaises(ValueError): self.physics.step(iteration=0)

    def test_failed_solver_never_returns_mock_or_previous_success(self):
        with patch('pandapower.runpp',side_effect=RuntimeError('no convergence')):
            with self.assertRaisesRegex(RuntimeError,'no convergence'):
                self.physics.step()
        self.assertFalse(self.physics.available)
        with self.assertRaises(RuntimeError): self.physics.get_state()

    def test_unknown_sensor_mapping_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            rt=prepare(tmp,mutate=lambda c:c['physics']['sensors']['V_CONTROL'].update(element='missing'))
            with self.assertRaisesRegex(ValueError,'Unknown bus'):
                create_physics_adapter(rt)

    def test_missing_declared_sensor_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            rt=prepare(tmp,mutate=lambda c:c['physics']['sensors'].pop('V_CONTROL'))
            with self.assertRaisesRegex(ValueError,'Unmapped physical sensors'):
                create_physics_adapter(rt)

    def test_disconnected_voltage_requires_explicit_missing_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            rt=prepare(tmp,mutate=lambda c:c['physics']['sensors']['V_LOAD'].pop('missing'))
            power=create_physics_adapter(rt)
            try:
                with self.assertRaisesRegex(RuntimeError,'Nonfinite result'):
                    power.step({'LOAD_SW':False})
            finally: power.close()

    def test_load_profile_sample_zero_and_next_step(self):
        with tempfile.TemporaryDirectory() as tmp:
            model=yaml.safe_load((POWER.parent/'model.yaml').read_text())
            model['load_profiles']={'flexible_load':[.5,.75]}
            model_path=Path(tmp)/'model.yaml'; model_path.write_text(yaml.safe_dump(model))
            rt=prepare(tmp,mutate=lambda c:c['physics'].update(model_file=str(model_path)))
            power=create_physics_adapter(rt)
            try:
                self.assertAlmostEqual(power.get_state()['P_LOAD'],.4)
                self.assertAlmostEqual(power.step()['values']['P_LOAD'],.6)
            finally: power.close()

    def test_generic_exports_include_time_backend_and_electrical_columns(self):
        runtime=self.rt.output_dir/'runtime'
        persist_physics_snapshot(runtime,self.rt,0,self.physics.initialize())
        persist_physics_snapshot(runtime,self.rt,1,self.physics.step({'LOAD_SW':False}))
        outputs=export_all(self.rt.config_path)
        with outputs['physics_state'].open() as f:
            rows=list(csv.DictReader(f))
        self.assertEqual(rows[1]['backend'],'pandapower')
        self.assertEqual(float(rows[1]['sim_time']),1)
        self.assertEqual(float(rows[1]['P_LOAD']),0)
        self.assertEqual(rows[1]['LOAD_SW'],'0')

    def test_real_modbus_frame_bias_drives_reference_control_and_power_flow(self):
        # Component test: real existing frame processor + true load flow.
        # This is explicitly NOT an OpenPLC/ns-3 end-to-end experiment.
        plc=self.rt.plcs['PLC2']; md=plc.md_vars['PLC1_V_CONTROL'].md_index
        value=self.physics.get_state()['V_CONTROL']
        rule=AttackRule('voltage_bias','PLC2','PLC1_V_CONTROL','request','add',-.15,{16},md_index=md,register=2048+2*md)
        events=EventWriter(self.rt.output_dir/'runtime/csv/attack_events.csv')
        state=AttackStateReader(None,default_active=False)
        processor=ModbusMitmProcessor(attack_name='power_voltage_bias',target='PLC2',rules=[rule],events=events,state=state)
        frame=struct.pack('>HHHBBHHBf',1,0,11,1,16,2048+2*md,2,4,value)
        transparent=processor.process(frame,'request',ConnectionState(),'scada','PLC2')
        self.assertEqual(transparent,frame)
        state.active=True
        altered=processor.process(frame,'request',ConnectionState(),'scada','PLC2')
        observed=struct.unpack('>f',altered[-4:])[0]
        self.assertLess(observed,.95)
        self.assertEqual(altered[:13],frame[:13])
        commands={'LOAD_SW':not(observed<.95)} # Reference rule; no PLC execution claimed.
        result=self.physics.step(commands)
        self.assertEqual(result['values']['P_LOAD'],0)
        self.assertEqual(rule.events,1)
        self.assertIn('bias',events.path.read_text())

    def test_runtime_dry_run_initializes_configured_backend(self):
        result=subprocess.run([sys.executable,'-m','src.runtime.persistent_closed_loop',
            '--config',str(self.rt.config_path),'--dry-run','--no-root-check'],
            cwd=ROOT,capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('available=True',result.stdout)
        self.assertIn('no control/network execution',result.stdout)

    def test_close_is_idempotent_and_prevents_reuse(self):
        self.physics.close(); self.physics.close()
        with self.assertRaises(RuntimeError): self.physics.get_state()
        with self.assertRaises(RuntimeError): self.physics.initialize()


if __name__=='__main__': unittest.main()
