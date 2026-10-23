"""Numerical gates using the installed public model and real AC solver."""
import importlib.util
from pathlib import Path
import unittest
import yaml

from src.physics.adapters.power_models import load_builtin, bus_map, power_metrics, resolve_load
ROOT=Path(__file__).resolve().parents[1]
MODEL=yaml.safe_load((ROOT/'examples/ieee33_bw/model.yaml').read_text())
HAVE_POWER=importlib.util.find_spec('pandapower') is not None


@unittest.skipUnless(HAVE_POWER, 'optional pandapower not installed')
class Ieee33ModelTests(unittest.TestCase):
    def test_original_topology_and_numerics(self):
        import pandapower as pp
        net,names,p=load_builtin(MODEL)
        self.assertEqual((len(net.bus),len(net.line),len(net.load),len(net.ext_grid),len(net.switch)),(33,37,32,1,0))
        self.assertEqual(int(net.line.in_service.sum()),32)
        self.assertTrue(all(net.bus.vn_kv==12.66))
        self.assertAlmostEqual(net.load.p_mw.sum(),3.715)
        self.assertAlmostEqual(net.load.q_mvar.sum(),2.3)
        pp.runpp(net,numba=False,tolerance_mva=1e-10)
        self.assertTrue(net.converged)
        self.assertAlmostEqual(net.res_bus.vm_pu.min(),.9131,delta=5e-5)
        self.assertAlmostEqual(power_metrics(net)['p_loss_mw'],.20266,delta=5e-5)
        v=net.res_bus.at[names['bus']['BUS_02'],'vm_pu']
        self.assertTrue(.95<v<1.03 and v-.1<.95)
        self.assertEqual(len(p['input_tables_sha256']),64)
        inverse={m['index']:m['paper_bus_number'] for m in p['bus_id_map'].values()}
        self.assertEqual({(inverse[int(r.from_bus)],inverse[int(r.to_bus)]) for _,r in net.line[~net.line.in_service].iterrows()},
                         {(21,8),(9,15),(12,22),(18,33),(25,29)})

    def test_mapping_uses_names_not_dataframe_indices(self):
        import pandapower as pp
        net,_,_=load_builtin(MODEL)
        pp.reindex_buses(net, {int(i):100+2*int(i) for i in net.bus.index})
        mapping=bus_map(net)
        self.assertEqual(mapping['BUS_24']['index'],146)
        self.assertEqual(mapping['BUS_24']['paper_bus_number'],24)
        net.bus.at[146,'name']='unexpected'
        with self.assertRaises(ValueError):bus_map(net)

    def test_unsupported_sources_and_parameter_overrides_rejected(self):
        for update in ({'case':'case14'},{'benchmark_mode':'modified'},{'loads':[]},
                       {'aliases':{'BUS_02':{'table':'bus','paper_bus_number':18}}},
                       {'controlled_elements':{'X':{'table':'load','bus_number':1,'field':'in_service'}}}):
            with self.subTest(update=update),self.assertRaises(ValueError):load_builtin({**MODEL,**update})

    def test_only_original_load24_changes(self):
        import pandapower as pp
        n,names,_=load_builtin(MODEL)
        i=names['load']['LOAD_BUS24'];before=n.load.copy(deep=True)
        self.assertAlmostEqual(n.load.at[i,'p_mw'],.420)
        self.assertAlmostEqual(n.load.at[i,'q_mvar'],.200)
        n.load.at[i,'in_service']=False;pp.runpp(n,numba=False)
        self.assertTrue(n.converged);self.assertEqual(n.res_load.at[i,'p_mw'],0.)
        self.assertAlmostEqual(power_metrics(n)['unserved_load_mw'],.420)
        for other in n.load.index.difference([i]):
            self.assertTrue(n.load.loc[other].equals(before.loc[other]))
            self.assertAlmostEqual(n.res_load.at[other,'p_mw'],n.load.at[other,'p_mw'])

    def test_ambiguous_load_never_selects_first_row(self):
        import pandapower as pp
        n,_,p=load_builtin(MODEL)
        pp.create_load(n,p['bus_id_map']['BUS_24']['index'],p_mw=.1,q_mvar=0.)
        with self.assertRaisesRegex(ValueError,'Ambiguous'):
            resolve_load(n,p['bus_id_map'],24)

    def test_aggregate_rejects_nonfinite_instead_of_skipping_nan(self):
        import pandapower as pp
        n,_,_=load_builtin(MODEL);pp.runpp(n,numba=False)
        n.res_load.at[n.res_load.index[0],'p_mw']=float('nan')
        with self.assertRaisesRegex(ValueError,'Nonfinite'):power_metrics(n)
