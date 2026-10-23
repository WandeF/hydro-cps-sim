"""UPSI contract, third-party SDK, physical timing and migration regressions."""
import copy
import csv
import importlib.util
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import yaml

from src.core.config import load_runtime_config
from src.physics.base import PhysicsBackend
from src.physics.types import BackendConfig, SignalSpec, StepResult
from src.physics.registry import register_backend, create_backend, configure_plugins, _BACKENDS
from src.physics.signal_mapper import SignalMapper
from src.physics.time_coordinator import TimeCoordinator
from src.physics.normalization import normalize_physics
from src.physics.session import PhysicsSession
from src.runtime.persistent_closed_loop import persist_physics_snapshot
from src.io.export_results import export_all
from test_modular_physics import prepare, ROOT

THERMAL = ROOT/'examples/thermal_box/config.yaml'


def prepare_v2(tmp, source=THERMAL, mutate=None):
    def configure(raw):
        raw['physics']['model']['path'] = str(source.parent/raw['physics']['model']['path'])
        if mutate: mutate(raw)
    return prepare(tmp,source,configure)


class ContractTests(unittest.TestCase):
    def test_legacy_model_path_falls_back_beside_a_copied_scene(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/'ctown_map.inp').write_bytes((ROOT/'examples/c_town/ctown_map.inp').read_bytes())
            raw=yaml.safe_load((ROOT/'examples/c_town/config.yaml').read_text())
            raw['inp_file']='/missing/old-install/ctown_map.inp'
            normalized=normalize_physics(raw,root/'config.yaml')
            self.assertEqual(normalized['model']['path'],str(root/'ctown_map.inp'))
            self.assertEqual(normalized['signals']['observations']['J129']['unit'],'mH2O')

    def test_water_metadata_does_not_treat_pressure_exponent_as_a_unit(self):
        from src.physics.legacy.ctown_compat import epanet_units
        self.assertEqual(epanet_units(ROOT/'examples/c_town/ctown_map.inp'),('LPS','m','mH2O'))
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'model.inp'
            path.write_text('[OPTIONS]\nPRESSURE kPa\nUNITS GPM\nPRESSURE EXPONENT 0.5\n')
            self.assertEqual(epanet_units(path),('GPM','ft','kPa'))

    def test_scalar_types_bounds_and_nonfinite_measurements(self):
        for dtype,value in [('float',True),('int',1.0),('bool',1),('str',0),('float',float('nan'))]:
            with self.subTest(dtype=dtype), self.assertRaises(ValueError):
                SignalSpec('x','output',dtype).validate(value)
        self.assertEqual(SignalSpec('label','output','str').validate('running'),'running')
        self.assertEqual(SignalSpec('count','input','int').validate(3),3)
        with self.assertRaises(ValueError): SignalSpec('x','input','float','W',0,1).validate(2.)
        with self.assertRaises(ValueError): SignalSpec('x','output','bool','1',0,1)

    def test_units_transform_both_directions_and_mapping_validation(self):
        ports={'sensor':SignalSpec('sensor','output','float','degC'),
               'heater':SignalSpec('heater','input','float','W',0,1000)}
        mapping={'observations':{'TEMP':{'port':'sensor','unit':'K','offset':273.15}},
                 'commands':{'HEAT':{'port':'heater','unit':'kW','scale':.001}}}
        mapper=SignalMapper(mapping,ports)
        result=StepResult(2.,{'sensor':20.},{'heater':500.})
        result.validate(ports)
        self.assertAlmostEqual(mapper.outputs(result)[0]['TEMP'],293.15)
        self.assertEqual(mapper.inputs({'HEAT':.5}),{'heater':500.})
        invalid=copy.deepcopy(mapping); invalid['observations']['TEMP'].pop('offset')
        with self.assertRaisesRegex(ValueError,'unit change'): SignalMapper(invalid,ports)
        invalid=copy.deepcopy(mapping); invalid['commands']['HEAT']['dtype']='bool'
        with self.assertRaisesRegex(ValueError,'dtype'): SignalMapper(invalid,ports)
        invalid=copy.deepcopy(mapping); invalid['commands']['OTHER']={'port':'heater'}
        with self.assertRaisesRegex(ValueError,'Duplicate command'): SignalMapper(invalid,ports)
        with self.assertRaises(ValueError): mapper.inputs({'HEAT':2.})

    def test_stale_quality_and_incomplete_results_fail(self):
        ports={'x':SignalSpec('x','output','float')}
        for result in (StepResult(2.,{'x':1.},{},timestamps={'x':1.}),
                       StepResult(2.,{'x':1.},{},quality={'x':'invalid'}),
                       StepResult(2.,{'x':1.},{},quality={'x':'initial'}),
                       StepResult(2.,{},{}), StepResult(2.,{'x':1.},{},status='error')):
            with self.subTest(result=result), self.assertRaises((ValueError,RuntimeError)):
                result.validate(ports)

    def test_time_grants_support_internal_events_and_reject_stalls(self):
        result=StepResult(0.,{},{}); coordinator=TimeCoordinator(300,'event_step')
        coordinator.initialize(result)
        self.assertEqual(coordinator.target(),300)
        grant=coordinator.accept(StepResult(45.,{},{}),300)
        self.assertEqual(grant['input_effective_time_s'],0)
        self.assertEqual(coordinator.target(),345)
        for actual in (45.,400.,float('nan')):
            with self.assertRaises(RuntimeError): coordinator.accept(StepResult(actual,{},{}),345)
        strict=TimeCoordinator(.002);strict.initialize(result)
        with self.assertRaises(RuntimeError): strict.accept(StepResult(.001,{},{}),.002)

    def test_registry_duplicates_unknown_and_invalid_implementations(self):
        with patch.dict(_BACKENDS):
            register_backend('test_sdk',lambda cfg: object())
            with self.assertRaises(ValueError): register_backend('test_sdk',lambda cfg: object())
            with self.assertRaises(TypeError): create_backend('test_sdk',None)
            with self.assertRaises(ValueError): create_backend('not_installed',None)
            with self.assertRaises(ValueError): configure_plugins({'physics':{'plugins':{'test_sdk':{'factory':'wrong:Class'}}}})
            with self.assertRaises(ValueError): register_backend('epanet',lambda cfg: object())

    def test_schema_requires_explicit_types_timing_and_supported_version(self):
        raw=yaml.safe_load(THERMAL.read_text())
        for mutate in (lambda c:c.update(schema_version=3),
                       lambda c:c['physics'].pop('timing'),
                       lambda c:c['signals']['HEATER'].pop('dtype'),
                       lambda c:c['signals']['HEATER'].update(direction='output')):
            item=copy.deepcopy(raw);mutate(item)
            with self.assertRaises(ValueError): normalize_physics(item,THERMAL)


class SDKTests(unittest.TestCase):
    def test_explicit_installed_entry_point_loads_a_real_adapter(self):
        with tempfile.TemporaryDirectory() as tmp,patch.dict(_BACKENDS):
            root=Path(tmp)
            dist=root/'upsi_sdk_test-0.0.1.dist-info';dist.mkdir()
            (dist/'METADATA').write_text('Metadata-Version: 2.1\nName: upsi-sdk-test\nVersion: 0.0.1\n')
            (dist/'entry_points.txt').write_text('[hydro_cps.physics]\nthermal_sdk_test = examples.thermal_box.adapter:ThermalBackend\n')
            with patch.object(sys,'path',[str(root)]+sys.path):
                configure_plugins({'physics':{'plugins':{'entry_test':{'entry_point':'thermal_sdk_test'}}}})
                config=BackendConfig(THERMAL,root,{'path':'model.yaml'}, {},1.,{'heater.enabled':False})
                backend=create_backend('entry_test',config)
                try:
                    self.assertIsInstance(backend,PhysicsBackend)
                    self.assertEqual(backend.initialize().outputs['box.temperature'],19.)
                finally:backend.close()

    def test_third_adapter_import_is_configuration_driven_without_solver_imports(self):
        code="""
import sys, yaml
from pathlib import Path
from src.physics.registry import configure_plugins, create_backend, _BACKENDS
from src.physics.types import BackendConfig
assert 'thermal_box' not in _BACKENDS
raw=yaml.safe_load(Path(sys.argv[1]).read_text())
configure_plugins(raw)
cfg=BackendConfig(Path(sys.argv[1]),Path(sys.argv[2]),raw['physics']['model'],raw['physics'],1.,{'heater.enabled':False})
backend=create_backend('thermal_box',cfg)
assert backend.initialize().outputs['box.temperature']==19.
assert 'epynet' not in sys.modules and 'pandapower' not in sys.modules
backend.close()
"""
        with tempfile.TemporaryDirectory() as tmp:
            result=subprocess.run([sys.executable,'-c',code,str(THERMAL),tmp],cwd=ROOT,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)

    def test_diagnostics_cannot_overwrite_canonical_snapshots(self):
        with tempfile.TemporaryDirectory() as tmp:
            session=PhysicsSession(prepare_v2(tmp))
            diagnostics={'values':{'TEMPERATURE':9999},'sim_time':1234,'status':'error',
                         'iteration':77,'quality':{'TEMPERATURE':'invalid'},
                         'legacy_snapshot':{'values':{'TEMPERATURE':9999},'sim_time':5678,'backend':'spoof'}}
            result=StepResult(1.,{'box.temperature':23.,'box.heater_power':0.},
                              {'heater.enabled':False},diagnostics=diagnostics)
            try:
                with patch.object(session.backend,'advance',return_value=result):
                    snapshot=session.advance({'HEATER':False},1)
                self.assertEqual(snapshot['values']['TEMPERATURE'],23.)
                self.assertEqual(snapshot['sim_time'],1.)
                self.assertEqual(snapshot['iteration'],1)
                self.assertEqual(snapshot['status'],'ok')
                self.assertEqual(snapshot['backend'],'thermal_box')
                self.assertEqual(snapshot['diagnostics'],diagnostics)
                snapshot['values']['TEMPERATURE']=0
                snapshot['diagnostics']['values']['TEMPERATURE']=0
                result.diagnostics['sim_time']=0
                self.assertEqual(session.initialize()['values']['TEMPERATURE'],23.)
                self.assertEqual(session.initialize()['diagnostics']['sim_time'],1234)
            finally:session.close()

    def test_real_thermal_ode_and_explicit_measurement_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            rt=prepare_v2(tmp);session=PhysicsSession(rt)
            try:
                initial=session.initialize();self.assertEqual(initial['values']['TEMPERATURE'],19.)
                advanced=session.advance({'HEATER':True},1)
                expected=26.+(19.-26.)*math.exp(-.1)
                self.assertAlmostEqual(advanced['values']['TEMPERATURE'],expected)
                self.assertEqual(advanced['values']['HEATER_POWER'],800.)
                self.assertEqual(advanced['quality']['TEMPERATURE'],'good')
                self.assertEqual(advanced['measurement_times']['TEMPERATURE'],1.)
                self.assertEqual(advanced['time_coordination']['input_effective_time_s'],0.)
                advanced['values']['TEMPERATURE']=0
                self.assertGreater(session.initialize()['values']['TEMPERATURE'],19.)
            finally:session.close();session.close()

    def test_sdk_supports_analog_inputs_independent_of_modbus(self):
        from examples.thermal_box.adapter import ThermalBackend
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'model.yaml'; model=yaml.safe_load((THERMAL.parent/'model.yaml').read_text())
            model['input_dtype']='float';path.write_text(yaml.safe_dump(model))
            config=BackendConfig(Path(tmp)/'scene.yaml',Path(tmp),{'path':'model.yaml'}, {},1.,{'heater.enabled':.5})
            backend=ThermalBackend(config)
            try:
                backend.initialize(); result=backend.advance(.5,{'heater.enabled':.5})
                result.validate(backend.describe()['signals'])
                self.assertEqual(result.outputs['box.heater_power'],400.)
                self.assertAlmostEqual(result.outputs['box.temperature'],22+(19-22)*math.exp(-.05))
                with self.assertRaises(ValueError): backend.advance(1.,{'heater.enabled':1.1})
            finally:backend.close()

    def test_generic_exports_preserve_units_quality_times_without_name_inference(self):
        with tempfile.TemporaryDirectory() as tmp:
            rt=prepare_v2(tmp);session=PhysicsSession(rt)
            try:
                for i,snapshot in enumerate((session.initialize(), session.advance({'HEATER':True},1))):
                    persist_physics_snapshot(rt.output_dir/'runtime',rt,i,snapshot)
                outputs=export_all(rt.config_path)
                schema=json.loads(outputs['signal_schema'].read_text())
                self.assertEqual(schema['signals']['TEMPERATURE']['unit'],'degC')
                with outputs['physics_signals'].open() as stream: rows=list(csv.DictReader(stream))
                row=next(r for r in rows if r['iteration']=='1' and r['signal']=='TEMPERATURE')
                self.assertEqual(row['quality'],'good');self.assertEqual(row['measurement_time_s'],'1.0')
                self.assertEqual(row['dtype'],'float');self.assertEqual(row['unit'],'degC')
                with outputs['physics_state'].open() as stream: states=list(csv.DictReader(stream))
                self.assertEqual(states[1]['HEATER'],'1')
                self.assertNotIn('T1',states[1])
            finally:session.close()

    def test_out_of_range_measurement_logs_last_valid_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            rt=prepare_v2(tmp,mutate=lambda raw:raw['signals']['TEMPERATURE'].update(maximum=19.5))
            session=PhysicsSession(rt)
            with self.assertRaisesRegex(ValueError,'above maximum'):
                session.advance({'HEATER':True},1)
            error=json.loads((rt.output_dir/'runtime/raw/physics_errors.jsonl').read_text())
            self.assertEqual(error['last_valid_time_s'],0.)
            self.assertFalse(session.available)

    def test_backend_failure_or_bad_timestamp_invalidates_session_and_is_logged(self):
        for failure in ('exception','stale','time_stall','status'):
            with self.subTest(failure=failure),tempfile.TemporaryDirectory() as tmp:
                rt=prepare_v2(tmp);session=PhysicsSession(rt)
                bad=copy.deepcopy(session.result)
                bad.time_s=1.
                if failure=='stale':bad.timestamps['box.temperature']=0.
                elif failure=='time_stall':bad.time_s=0.
                elif failure=='status':bad.status='error'
                kwargs={'side_effect':RuntimeError('solver failed')} if failure=='exception' else {'return_value':bad}
                with patch.object(session.backend,'advance',**kwargs):
                    with self.assertRaises((RuntimeError,ValueError)):
                        session.advance({'HEATER':False},1)
                self.assertFalse(session.available)
                with self.assertRaises(RuntimeError):session.initialize()
                errors=rt.output_dir/'runtime/raw/physics_errors.jsonl'
                self.assertEqual(json.loads(errors.read_text().splitlines()[-1])['status'],'error')
                session.close()

    def test_runtime_uses_third_adapter_without_known_backend_cli_choices(self):
        with tempfile.TemporaryDirectory() as tmp:
            rt=prepare_v2(tmp)
            result=subprocess.run([sys.executable,'-m','src.runtime.persistent_closed_loop','--config',str(rt.config_path),
                                  '--dry-run','--no-root-check'],cwd=ROOT,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertIn('available=True',result.stdout)


@unittest.skipUnless(importlib.util.find_spec('epynet'),'epynet is optional')
class WaterUPSIRegression(unittest.TestCase):
    def test_upsi_matches_legacy_engine_through_internal_events(self):
        from src.physics.engine import PhysicsEngine
        with tempfile.TemporaryDirectory() as tmp:
            rt=prepare(tmp, ROOT/'examples/c_town/config.yaml')
            session=PhysicsSession(rt)
            original=PhysicsEngine(rt,mode='dhalsim_epynet',work_dir=Path(tmp)/'old')
            try:
                for i in range(2,25):
                    commands=dict(rt.actuator_initial_state)
                    if i>=7:commands['PU1']=True
                    old=original.step(commands,i);new=session.advance(commands,i)
                    self.assertEqual(old['sim_time'],new['sim_time'])
                    self.assertEqual(old['actuators_applied'],new['actuators_applied'])
                    for name,value in new['values'].items():self.assertAlmostEqual(old['values'][name],value)
            finally:session.close();original.close()

    def test_v2_explicit_water_ports_allow_arbitrary_logical_names(self):
        def configure(raw):
            normalized=normalize_physics(raw,ROOT/'examples/c_town/config.yaml')
            observations=normalized['signals']['observations']
            renamed={name:f'READING_{i}' for i,name in enumerate(observations)}
            for actuator in raw['actuators']:
                actuator['initial_state']=str(actuator['initial_state']).lower() in {'open','true','on','1'}
            raw['schema_version']=2
            raw['physics']={k:normalized[k] for k in ('backend','model','timing','signals')}
            raw['physics']['signals']['observations']={renamed[k]:v for k,v in observations.items()}
            for plc in raw['plcs']:
                plc['sensors']=[renamed[k] for k in plc['sensors']];plc['controls']=[]
        with tempfile.TemporaryDirectory() as tmp:
            rt=prepare(tmp,ROOT/'examples/c_town/config.yaml',configure)
            session=PhysicsSession(rt)
            try:
                self.assertEqual(session.start_cycle,0)
                result=session.advance(rt.actuator_initial_state,1)
                self.assertTrue(all(name.startswith('READING_') for name in result['values']))
                self.assertEqual(result['sim_time'],300.)
            finally:session.close()


@unittest.skipUnless(importlib.util.find_spec('pandapower'),'pandapower is optional')
class PowerUPSIRegression(unittest.TestCase):
    def test_legacy_inverted_bool_mapping_preserves_physics(self):
        with tempfile.TemporaryDirectory() as tmp:
            rt=prepare(tmp,mutate=lambda raw:raw['physics']['actuators']['LOAD_SW'].update(invert=True))
            session=PhysicsSession(rt)
            try:
                self.assertEqual(session.initialize()['values']['P_LOAD'],0)
                self.assertAlmostEqual(session.advance({'LOAD_SW':False},1)['values']['P_LOAD'],.8)
            finally:session.close()

    def test_v2_real_power_flow_breaker_and_unsupplied_quality(self):
        with tempfile.TemporaryDirectory() as tmp:
            rt=prepare_v2(tmp,ROOT/'examples/power_feeder/config_v2.yaml');session=PhysicsSession(rt)
            try:
                self.assertAlmostEqual(session.initialize()['values']['P_LOAD'],.8)
                result=session.advance({'LOAD_SW':False},1)
                self.assertEqual(result['values']['P_LOAD'],0)
                self.assertEqual(result['quality']['V_LOAD'],'unsupplied')
                self.assertTrue(result['solver_converged'])
            finally:session.close()
