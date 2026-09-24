import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.core.config import RuntimeConfig
from src.check.delivered_inputs import verify_delivered_inputs


class DeliveredInputs(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.output = Path(temporary.name)
        self.source = self.output / 'runtime/json'
        self.source.mkdir(parents=True)
        self.rt = RuntimeConfig(
            config_path=self.output / 'config.yaml', output_dir=self.output,
            st_dir=self.output / 'st', plcs={}, initial_state={'T4': 2.5},
            actuator_initial_state={'PU7': True}, hydraulic_timestep=300, iterations=2,
            raw={'plcs': [
                {'name': 'PLC5', 'sensors': [], 'actuators': ['PU7'], 'controls': [
                    {'actuator': 'PU7', 'dependant': 'T4', 'type': 'below', 'value': 3.0, 'action': 'open'},
                    {'actuator': 'PU7', 'dependant': 'T4', 'type': 'above', 'value': 4.5, 'action': 'closed'}]},
                {'name': 'PLC7', 'sensors': ['T4'], 'actuators': [], 'controls': []}]})
        loader = patch('src.check.delivered_inputs.load_runtime_config', return_value=self.rt)
        loader.start()
        self.addCleanup(loader.stop)
        self.write('physics_0001.json', {'values': {'T4': 4.6}})
        self.write('actuator_state_0001.json', {'PU7': True})
        self.write('local_write_0001_plc5.json', {'written': {}})
        self.delivery = {'status': 'ok', 'errors': [], 'timeout': False,
                         'written': {'PLC7_T4': {'value': 3.177574}}}

    def write(self, name, value):
        (self.source / name).write_text(json.dumps(value))

    def audit(self):
        self.write('scada_downlink_0001.json', {'plcs': {'PLC5': self.delivery}})
        return verify_delivered_inputs(self.rt.config_path)

    def test_stale_delivered_value_explains_nominal_deviation(self):
        result = self.audit()
        self.assertTrue(result['ok'])
        self.assertEqual(len(result['nominal_deviations']), 1)
        self.assertEqual(result['nominal_deviations'][0]['physical_inputs'], {'T4': 4.6})

    def test_wrong_action_is_not_excused_by_dos(self):
        self.write('actuator_state_0001.json', {'PU7': False})
        self.assertFalse(self.audit()['ok'])

    def test_timeout_does_not_prove_delivery(self):
        self.delivery['status'] = 'timeout'
        self.assertFalse(self.audit()['ok'])

    def test_missing_input_does_not_fall_back_to_physical_truth(self):
        self.delivery['written'] = {}
        self.assertFalse(self.audit()['ok'])

    def test_missing_actuator_state_is_rejected(self):
        self.write('actuator_state_0001.json', {})
        self.assertFalse(self.audit()['ok'])

    def test_real32_rounding_matches_plc_comparison(self):
        self.rt.actuator_initial_state['PU7'] = False
        self.write('actuator_state_0001.json', {'PU7': False})
        self.delivery['written']['PLC7_T4']['value'] = 2.999999999
        self.assertTrue(self.audit()['ok'])


if __name__ == '__main__':
    unittest.main()
