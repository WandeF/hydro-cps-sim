"""Real IEEE33 UPSI, configuration and failure contracts (no mock solvers)."""
from pathlib import Path
import tempfile
import unittest
from test_ieee33_model import ROOT,HAVE_POWER
from test_modular_physics import prepare
from src.physics.session import PhysicsSession

SCENE=ROOT/'examples/ieee33_bw/config_baseline.yaml'

def config(tmp,mutate=None):
    def edit(raw):
        raw['physics']['model']['path']=str(SCENE.parent/'model.yaml')
        if mutate:mutate(raw)
    return prepare(tmp,SCENE,edit)


@unittest.skipUnless(HAVE_POWER,'optional pandapower not installed')
class Ieee33UpSiTests(unittest.TestCase):
    def test_real_commands_clock_quality_units_snapshot_ownership(self):
        with tempfile.TemporaryDirectory() as tmp:
            session=PhysicsSession(config(tmp))
            try:
                initial=session.initialize()
                self.assertAlmostEqual(initial['values']['P_LOAD24'],.420)
                self.assertTrue(initial['diagnostics']['solver_converged'])
                self.assertEqual(initial['signal_metadata']['V_BUS02']['unit'],'pu')
                self.assertEqual(initial['signal_metadata']['P_LOAD24']['unit'],'MW')
                initial['diagnostics']['power_metrics']['served_load_mw']=999
                initial['values']['P_LOAD24']=999
                self.assertAlmostEqual(session.initialize()['values']['P_LOAD24'],.420)
                result=session.advance({'LOAD24_ENABLE':False},iteration=1)
                self.assertEqual(result['sim_time'],1.)
                self.assertEqual(result['time_coordination']['input_effective_time_s'],0.)
                self.assertEqual(result['values']['P_LOAD24'],0.)
                self.assertAlmostEqual(result['values']['UNSERVED_LOAD_P'],.420)
                self.assertEqual(result['quality']['V_BUS24'],'good') # bus remains energized
                self.assertTrue(result['validity']['V_BUS24'])
                result=session.advance({'LOAD24_ENABLE':True},iteration=2)
                self.assertEqual(result['sim_time'],2.)
                self.assertAlmostEqual(result['values']['P_LOAD24'],.420)
            finally:session.close()

    def test_nonbool_command_fails_and_closes(self):
        with tempfile.TemporaryDirectory() as tmp:
            session=PhysicsSession(config(tmp))
            with self.assertRaises(ValueError):session.advance({'LOAD24_ENABLE':1.},iteration=1)
            self.assertTrue(session.closed)

    def test_actual_nonconvergence_never_published_as_good(self):
        with tempfile.TemporaryDirectory() as tmp:
            def edit(raw):raw['physics']['solver']['max_iteration']=1
            with self.assertRaisesRegex(RuntimeError,'solve failed'):PhysicsSession(config(tmp,edit))
            self.assertFalse((Path(tmp)/'output/runtime/json/signal_schema.json').exists())

    def test_island_requires_quality_marked_voltage(self):
        with tempfile.TemporaryDirectory() as tmp:
            session=PhysicsSession(config(tmp))
            try:
                a=session.backend.adapter
                a.net.line.loc[:,'in_service']=False
                with self.assertRaisesRegex(RuntimeError,'Nonfinite result'):
                    session.advance({'LOAD24_ENABLE':True},iteration=1)
                self.assertTrue(session.closed)
            finally:session.close()

    def test_invalid_pu_unit_conversion_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            def edit(raw):raw['physics']['signals']['observations']['V_BUS02']['unit']='V'
            with self.assertRaises(ValueError):PhysicsSession(config(tmp,edit))

    def test_disconnected_bus_zero_is_explicitly_unsupplied(self):
        with tempfile.TemporaryDirectory() as tmp:
            def edit(raw):
                for tag,spec in raw['physics']['signals']['observations'].items():
                    if tag.startswith('V_BUS'):spec['missing']='zero_if_unsupplied'
            session=PhysicsSession(config(tmp,edit))
            try:
                session.backend.adapter.net.line.loc[:,'in_service']=False
                result=session.advance({'LOAD24_ENABLE':True},iteration=1)
                self.assertTrue(result['diagnostics']['solver_converged'])
                self.assertEqual(result['values']['V_BUS24'],0.)
                self.assertEqual(result['quality']['V_BUS24'],'unsupplied')
                buses=result['diagnostics']['bus_voltages']
                self.assertIsNone(next(b for b in buses if b['bus_number']==24)['vm_pu'])
            finally:session.close()

    def test_all_cases_share_topology_and_generated_dependency(self):
        import yaml
        from src.control.st_generation import generate_plc_st
        from src.network.ns3_generation import generate_cc
        scenes=[yaml.safe_load((SCENE.parent/f'config_{c}.yaml').read_text()) for c in ('baseline','transparent','mitm')]
        self.assertTrue(scenes[0]['network']==scenes[1]['network']==scenes[2]['network'])
        for raw in scenes:
            st=generate_plc_st(raw,raw['plcs'][2])
            self.assertIn('PLC1_V_BUS02',st)
            self.assertIn('PLC3_LOAD24_ENABLE',st)
            cc=generate_cc(raw)
            self.assertIn('PLC3',cc)
