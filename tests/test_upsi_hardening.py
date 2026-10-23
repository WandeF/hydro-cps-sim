"""Rootless boundary/fault fixtures; no real Modbus endpoints or old outputs."""
import copy
from concurrent.futures import ThreadPoolExecutor
import json
import math
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.physics.base import PhysicsBackend
from src.physics.types import StepResult, SignalSpec, BackendConfig
from src.physics.registry import configure_plugins, create_backend, _BACKENDS
from src.physics.quality import QualityGate, QualityError, source_metadata, validate_policy
from src.physics.session import PhysicsSession
from src.physics.signal_mapper import SignalMapper
from src.physics.time_coordinator import TimeCoordinator
from src.physics.contract import exercise_backend
from src.core.config import write_json, load_runtime_config
from src.plc.adapter import _write_sensors_with_client
from src.scada.client import _prepare_downlink_writes, annotate_poll, _mark_timeout
from test_upsi import prepare_v2, THERMAL


class FixtureBackend(PhysicsBackend):
    """SDK fixture intentionally supports fault injection, not a physical model."""
    def __init__(self):
        self.closed=0;self.initialize_calls=0;self.close_error=False;self.init_error=False
    def describe(self):
        return {'signals':{'x':SignalSpec('x','output','float'),'u':SignalSpec('u','input','bool')},'timing':'target'}
    def initialize(self):
        self.initialize_calls+=1
        if self.init_error: raise RuntimeError('primary initialization failure')
        return StepResult(0.,{'x':20.},{'u':False})
    def advance(self,until_s,inputs):
        if self.closed:raise RuntimeError('closed')
        return StepResult(until_s,{'x':23.},dict(inputs))
    def close(self):
        self.closed+=1
        if self.close_error:raise OSError('cleanup failure')


class OtherThermal:
    def __new__(cls,cfg):
        from examples.thermal_box.adapter import ThermalBackend
        backend=ThermalBackend(cfg);backend.identity='other';return backend


def parallel_csv_writer(path, worker):
    from src.io.csv import append_row
    for i in range(20):append_row(Path(path),{'worker':worker,'sequence':i,f'value_{worker}':worker+i})


class HardeningContractTests(unittest.TestCase):
    def test_rootless_fixture_retains_primary_and_cleanup_exceptions(self):
        fixture=FixtureBackend();fixture.init_error=True;fixture.close_error=True
        diagnostics=[]
        with self.assertRaisesRegex(RuntimeError,'primary initialization') as caught:
            exercise_backend(fixture,{'u':False},1.,diagnostics=diagnostics)
        self.assertIsInstance(caught.exception.cleanup_error,OSError)
        self.assertEqual(diagnostics[0]['stage'],'close')

    def test_rootless_sdk_fixture_reinitializes_and_closes(self):
        fixture=FixtureBackend()
        samples=exercise_backend(fixture,{'u':False},1.,steps=2)
        self.assertEqual(len(samples),3);self.assertEqual(fixture.initialize_calls,2)
        self.assertEqual(fixture.closed,1)
        with self.assertRaises(RuntimeError):fixture.advance(3.,{'u':False})

    def test_historical_snapshot_fields_have_an_explicit_whitelist(self):
        from src.physics.legacy.ctown_compat import snapshot_compatibility_fields
        fields=snapshot_compatibility_fields({'backend':'dhalsim_epynet','legacy':True},
            {'legacy_snapshot':{'values':{'x':999},'status':'bad','sim_time':999,'link_flow':{'PU1':2.}},
             'internal_steps':1,'iteration':999})
        self.assertEqual(fields,{'link_flow':{'PU1':2.},'internal_steps':1})

    def test_snapshot_legacy_conflicts_do_not_own_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            session=PhysicsSession(prepare_v2(tmp))
            try:
                result=StepResult(1.,{'box.temperature':23.,'box.heater_power':0.},{'heater.enabled':False},
                    diagnostics={'time_coordination':{'granted_time_s':999},'validity':{'TEMPERATURE':False},
                                 'signal_metadata':{},'backend':'spoof','iteration':999})
                with patch.object(session.backend,'advance',return_value=result):sample=session.advance({'HEATER':False},1)
                self.assertEqual(sample['time_coordination']['granted_time_s'],1.)
                self.assertTrue(sample['validity']['TEMPERATURE']);self.assertEqual(sample['backend'],'thermal_box')
                self.assertTrue(sample['signal_metadata'])
            finally:session.close()

    def test_primary_initial_failure_survives_cleanup_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            rt=prepare_v2(tmp); fixture=FixtureBackend();fixture.init_error=True;fixture.close_error=True
            from src.physics.factory import backend_configuration
            cfg=backend_configuration(rt)
            fixture.describe=lambda:{'signals':{'box.temperature':SignalSpec('box.temperature','output','float','degC'),
                'box.heater_power':SignalSpec('box.heater_power','output','float','W'),
                'heater.enabled':SignalSpec('heater.enabled','input','bool')},'timing':'target'}
            with patch('src.physics.session.create_backend',return_value=(fixture,cfg)):
                with self.assertRaisesRegex(RuntimeError,'primary initialization'):PhysicsSession(rt)
            records=[json.loads(s) for s in (rt.output_dir/'runtime/raw/physics_errors.jsonl').read_text().splitlines()]
            self.assertEqual([r['stage'] for r in records],['initialize','close'])
            self.assertEqual(records[1]['exception_type'],'OSError');self.assertIn('primary_error',records[1])
            self.assertEqual(fixture.closed,1)

    def test_explicit_close_failure_is_recorded_and_not_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            session=PhysicsSession(prepare_v2(tmp))
            with patch.object(session.backend,'close',side_effect=OSError('close broke')) as close:
                with self.assertRaises(OSError):session.close()
                session.close();self.assertEqual(close.call_count,1)
            self.assertFalse(session.available);self.assertEqual(session.failure_records[-1]['stage'],'close')
            with self.assertRaises(RuntimeError):session.advance({'HEATER':False},1)

    def test_wrong_result_object_is_rejected_by_the_session_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            session=PhysicsSession(prepare_v2(tmp))
            with patch.object(session.backend,'advance',return_value={'time_s':1.,'outputs':{}}):
                with self.assertRaisesRegex(TypeError,'StepResult'):session.advance({'HEATER':False},1)
            self.assertFalse(session.available);self.assertEqual(session.failure_records[0]['stage'],'validate_result')

    def test_all_invalid_results_fail_without_replacing_snapshot(self):
        faults=[lambda r:r.outputs.pop('box.temperature'),lambda r:r.outputs.update(extra=1.),
                lambda r:r.outputs.update({'box.temperature':True}),lambda r:r.outputs.update({'box.temperature':float('nan')}),
                lambda r:r.applied_inputs.update({'heater.enabled':True}),lambda r:setattr(r,'status','error'),
                lambda r:setattr(r,'time_s',-1.),lambda r:setattr(r,'time_s',0.),
                lambda r:r.timestamps.update({'box.temperature':float('inf')})]
        for fault in faults:
            with self.subTest(fault=fault),tempfile.TemporaryDirectory() as tmp:
                session=PhysicsSession(prepare_v2(tmp));initial=session.initialize()
                r=StepResult(1.,{'box.temperature':23.,'box.heater_power':0.},{'heater.enabled':False});fault(r)
                with patch.object(session.backend,'advance',return_value=r):
                    with self.assertRaises((ValueError,RuntimeError)):session.advance({'HEATER':False},1)
                self.assertEqual(session.snapshot,initial);self.assertFalse(session.available)
                error=session.failure_records[0]
                for key in ('backend','stage','requested_time_s','last_valid_time_s','iteration','exception_type','wall_time_utc'):
                    self.assertIn(key,error)
                self.assertEqual(error['iteration'],1);self.assertEqual(error['last_valid_time_s'],0.)
                self.assertFalse(list((session.rt.output_dir/'runtime').rglob('physics_0001.ready')))

    def test_parallel_csv_header_expansion_preserves_every_row(self):
        import csv
        import multiprocessing
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'data.csv'
            processes=[multiprocessing.Process(target=parallel_csv_writer,args=(str(path),i)) for i in range(4)]
            for process in processes:process.start()
            for process in processes:process.join(10);self.assertEqual(process.exitcode,0)
            with path.open() as stream:rows=list(csv.DictReader(stream))
            self.assertEqual(len(rows),80)
            self.assertEqual({(int(r['worker']),int(r['sequence'])) for r in rows},{(w,i) for w in range(4) for i in range(20)})
            for row in rows:self.assertEqual(int(row[f"value_{row['worker']}"]),int(row['worker'])+int(row['sequence']))

    def test_atomic_json_preserves_prior_file_after_partial_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'state.json';write_json(path,{'values':{'x':20.}})
            def broken(obj,stream,**kwargs):stream.write('{"values":');raise OSError('disk failure')
            with patch('json.dump',side_effect=broken):
                with self.assertRaises(OSError):write_json(path,{'values':{'x':99.}})
            self.assertEqual(json.loads(path.read_text())['values']['x'],20.)
            self.assertEqual(list(Path(tmp).glob('*.tmp')),[])

    def test_registry_scene_names_do_not_pollute_global_and_parallel_load(self):
        def build(factory):
            raw={'physics':{'plugins':{'external_demo':{'factory':factory}}}}
            context=configure_plugins(raw)
            cfg=BackendConfig(THERMAL,Path('.'),{'path':'model.yaml'},{},1.,{'heater.enabled':False},registry=context)
            return create_backend('external_demo',cfg)
        factories=['examples.thermal_box.adapter:ThermalBackend','test_upsi_hardening:OtherThermal']
        before=dict(_BACKENDS)
        for pair in (list(map(build,factories)),list(ThreadPoolExecutor(2).map(build,factories))):
            self.assertFalse(hasattr(pair[0],'identity'));self.assertEqual(pair[1].identity,'other')
            for backend in pair:backend.close()
        self.assertEqual(_BACKENDS,before)
        with self.assertRaises(ValueError):configure_plugins({'physics':{'plugins':{'epanet':{'factory':factories[0]}}}})
        for bad in ('bad','module:',' x:y','module:../Class'):
            with self.assertRaises(ValueError):configure_plugins({'physics':{'plugins':{'x':{'factory':bad}}}})

    def test_units_dimensions_conversion_consistency_and_explicit_calibration(self):
        ports={'x':SignalSpec('x','output','float','W')}
        for unit,scale in [('m',1.),('kW',2.),('pu',1.)]:
            with self.assertRaises(ValueError):SignalMapper({'observations':{'X':{'port':'x','unit':unit,'scale':scale}}},ports)
        mapper=SignalMapper({'observations':{'X':{'port':'x','unit':'kW','scale':.001}}},ports)
        self.assertEqual(mapper.transforms['X'],'catalog_conversion')
        with self.assertRaises(ValueError):SignalMapper({'observations':{'X':{'port':'x','scale':2}}},ports)
        mapper=SignalMapper({'observations':{'X':{'port':'x','scale':2,'calibration_reason':'existing calibrated sensor'}}},ports)
        self.assertIn('calibration',mapper.transforms['X'])
        water={'p':SignalSpec('p','output','float','mH2O')}
        with self.assertRaises(ValueError):SignalMapper({'observations':{'P':{'port':'p','unit':'Pa','scale':9806.65}}},water)
        SignalMapper({'observations':{'P':{'port':'p','unit':'Pa','scale':9806.65,
            'unit_context':{'density_kg_m3':1000.,'gravity_m_s2':9.80665}}}},water)

    def test_quality_validity_and_bounded_measurement_capability(self):
        ports={'x':SignalSpec('x','output','float')}
        result=StepResult(1.,{'x':0.},{},quality={'x':'unsupplied'},validity={'x':False})
        result.validate(ports);self.assertFalse(result.validity['x']);self.assertEqual(result.freshness['x'],'fresh')
        result=StepResult(1.,{'x':20.},{},timestamps={'x':.9})
        with self.assertRaises(ValueError):result.validate(ports)
        result.validate(ports,measurement_max_age_s=.2);self.assertEqual(result.freshness['x'],'delayed')
        for quality in ('invalid','stale'):
            with self.assertRaises(RuntimeError):StepResult(1.,{'x':20.},{},quality={'x':quality}).validate(ports)
        with tempfile.TemporaryDirectory() as tmp:
            rt=prepare_v2(tmp,mutate=lambda c:c['physics']['timing'].update(measurement_max_age_s=.2))
            with self.assertRaisesRegex(ValueError,'advertise'):PhysicsSession(rt)

    def test_unrepresentable_time_request_invalidates_and_records_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            session=PhysicsSession(prepare_v2(tmp));session.time.time_s=1e300
            with self.assertRaisesRegex(RuntimeError,'resolution'):session.advance({'HEATER':False},1)
            self.assertFalse(session.available);self.assertEqual(session.failure_records[0]['stage'],'time_request')
            self.assertEqual(session.failure_records[0]['last_valid_time_s'],0.)

    def test_time_tolerance_small_steps_and_real_event_grants(self):
        for step in (.002,.1,1e-12):
            coordinator=TimeCoordinator(step);coordinator.initialize(StepResult(0.,{},{}))
            for _ in range(20):
                target=coordinator.target();coordinator.accept(StepResult(target,{},{}),target)
            self.assertAlmostEqual(coordinator.time_s,20*step)
        coordinator=TimeCoordinator(300,'event_step');coordinator.initialize(StepResult(0.,{},{}))
        coordinator.accept(StepResult(104.,{},{}),300.);self.assertEqual(coordinator.time_s,104.)
        for kwargs in ({'absolute_s':1000},{'relative':float('nan')},{'relative':1.}):
            with self.assertRaises(ValueError):TimeCoordinator(1.,**kwargs)
        with self.assertRaises(ValueError):coordinator.accept(StepResult(200.,{},{}),float('inf'))


class QualityBoundaryTests(unittest.TestCase):
    def plc(self):
        return SimpleNamespace(name='PLC1',ip='1.2.3.4',md_vars={
            'PLC1_TEMP':SimpleNamespace(name='PLC1_TEMP',tag='TEMP',md_index=1,source_prefix='PLC1')})

    def test_quality_states_default_hold_fail_skip_and_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'audit.jsonl'
            gate=QualityGate({'quality_policy':{'overrides':{'unsupplied':'hold_last','stale':'fail_fast'}}},path)
            meta={'quality':'good','valid':True,'source_time_s':0.,'freshness':'fresh'}
            value,record=gate.decide('TEMP',23.,meta,destination='PLC2',iteration=0,stage='test',source='physics')
            gate.finish([record],written=['TEMP']);self.assertEqual(value,23.)
            value,record=gate.decide('TEMP',0.,{**meta,'quality':'unsupplied'},destination='PLC2',iteration=1,stage='test',source='physics')
            self.assertEqual(value,23.);gate.finish([record],written=['TEMP']);self.assertEqual(record['delivered_value'],23.)
            self.assertEqual(record['delivered_source_time_s'],0.)
            self.assertEqual(record['delivered_quality'],'good')
            with self.assertRaises(QualityError):gate.decide('TEMP',23.,{**meta,'freshness':'stale'},destination='PLC2',iteration=2,stage='test',source='poll')
            self.assertEqual(len(path.read_text().splitlines()),3)
        for quality,valid,value in [('good',True,23.),('initial',False,19.),('unsupplied',True,0.)]:
            gate=QualityGate();selected,record=gate.decide('x',value,{'quality':quality,'valid':valid},destination='PLC1',iteration=0,stage='test',source='physics')
            self.assertEqual(selected,value)
        for value,quality in [(None,'missing'),(float('nan'),'good'),(3.,'invalid')]:
            selected,record=QualityGate().decide('x',value,{'quality':quality},destination='PLC1',iteration=0,stage='test',source='physics')
            self.assertIsNone(selected)
        value,record=QualityGate({'quality_policy':{'default':'hold_last'}}).decide('x',0.,{'quality':'unsupplied'},destination='PLC1',iteration=0,stage='test',source='physics')
        self.assertIsNone(value);self.assertIn('no previously',record['reason'])

    def test_invalid_and_conflicting_policy_configurations(self):
        for policy in ([],{'default':'bad'},{'default':[]},{'overrides':{'bad':'hold_last'}},{'overrides':{'good':[]}}):
            with self.assertRaises(ValueError):validate_policy({'quality_policy':policy})
        with self.assertRaises(ValueError):validate_policy({'quality_policy':{'default':'hold_last'},'physics':{'quality_policy':{}}})

    def test_local_write_keeps_metadata_and_commits_only_on_success(self):
        plc=self.plc();mb=SimpleNamespace(write_real_mds=lambda values:None)
        gate=QualityGate({'quality_policy':{'overrides':{'unsupplied':'hold_last'}}})
        snapshot={'sim_time':0.,'values':{'TEMP':23.},'quality':{'TEMP':'good'},'validity':{'TEMP':True}}
        first=_write_sensors_with_client(plc,mb,snapshot['values'],'local',snapshot=snapshot,gate=gate,iteration=0)
        self.assertEqual(first['measurement_metadata']['PLC1_TEMP']['source_time_s'],0.)
        snapshot.update(sim_time=1.,values={'TEMP':0.},quality={'TEMP':'unsupplied'})
        second=_write_sensors_with_client(plc,mb,snapshot['values'],'local',snapshot=snapshot,gate=gate,iteration=1)
        self.assertEqual(second['written']['PLC1_TEMP'],23.)
        self.assertEqual(second['measurement_metadata']['PLC1_TEMP']['freshness'],'stale')
        broken=SimpleNamespace(write_real_mds=lambda values:(_ for _ in ()).throw(OSError('write failed')))
        newgate=QualityGate()
        with self.assertRaises(OSError):_write_sensors_with_client(plc,broken,{'TEMP':23.},'local',gate=newgate)
        self.assertEqual(newgate.accepted,{})

    def test_timeout_cache_retains_source_state_and_age_across_plcs(self):
        owner=self.plc();destination=SimpleNamespace(name='PLC2',ip='2.3.4.5',md_vars=owner.md_vars)
        meta={'quality':'unsupplied','valid':True,'freshness':'fresh','source_time_s':1.}
        previous={'iteration':1,'md':{'PLC1_TEMP':0.},'measurement_metadata':{'PLC1_TEMP':meta}}
        item={'md':{},'coils':{},'errors':[]};_mark_timeout(item,'poll','timeout',previous)
        poll={'plcs':{'PLC1':item}}
        annotate_poll(SimpleNamespace(plcs={'PLC1':owner}),poll,{'sim_time':2.},{},{'PLC1':previous})
        actual=item['measurement_metadata']['PLC1_TEMP']
        self.assertEqual(actual['quality'],'unsupplied');self.assertEqual(actual['source_time_s'],1.)
        self.assertEqual(actual['network_freshness'],'stale')
        gate=QualityGate({'quality_policy':{'overrides':{'stale':'skip_write'}}})
        payload,writes=_prepare_downlink_writes(destination,poll,{'TEMP':23.},gate=gate,iteration=2)
        self.assertEqual(writes,[]);self.assertEqual(payload['quality_audit'][0]['quality'],'stale')
        payload,writes=_prepare_downlink_writes(destination,poll,{'TEMP':23.},iteration=2)
        self.assertEqual(writes[0][1],0.)  # Default preserves the original timeout-cache trajectory.
        self.assertEqual(payload['quality_audit'][0]['source_quality'],'unsupplied')

    def test_malformed_metadata_is_invalid_and_audited_as_standard_json(self):
        for metadata in ({'quality':[]},{'freshness':[]},{'network_freshness':[]},{'source_time_s':float('nan')},
                         {'source_time_s':0.,'observation_time_s':'bad'}):
            with self.subTest(metadata=metadata),tempfile.TemporaryDirectory() as tmp:
                path=Path(tmp)/'audit.jsonl';gate=QualityGate(audit_path=path)
                value,record=gate.decide('TEMP',23.,metadata,destination='PLC1',iteration=1,stage='test',source='physics')
                self.assertIsNone(value);self.assertEqual(record['quality'],'invalid')
                gate.finish([record])
                def reject(token):raise AssertionError(token)
                json.loads(path.read_text(),parse_constant=reject)

    def test_legacy_unknown_time_is_audited_and_can_be_rejected(self):
        meta=source_metadata({},'TEMP')
        self.assertEqual(meta['freshness'],'unknown')
        value,record=QualityGate().decide('TEMP',23.,meta,destination='PLC1',iteration=0,stage='test',source='legacy')
        self.assertEqual(value,23.);self.assertEqual(record['quality'],'stale')
        strict=QualityGate({'quality_policy':{'overrides':{'stale':'fail_fast'}}})
        with self.assertRaises(QualityError):strict.decide('TEMP',23.,meta,destination='PLC1',iteration=0,stage='test',source='legacy')

    def test_skip_and_missing_values_never_write_zero(self):
        values=[];mb=SimpleNamespace(write_real_mds=lambda data:values.append(data))
        payload=_write_sensors_with_client(self.plc(),mb,{},'local')
        self.assertEqual(payload['written'],{});self.assertEqual(values,[])
        payload=_write_sensors_with_client(self.plc(),mb,{'TEMP':23.},'local',gate=QualityGate({'quality_policy':{'default':'skip_write'}}))
        self.assertEqual(values,[]);self.assertEqual(payload['quality_audit'][0]['decision'],'skip_write')
