import copy
import json
import unittest
from unittest.mock import patch

from simulator import experience, reflex
from simulator.engine import Simulation
from simulator.happyrobot import HappyRobot


class FireProvenanceTests(unittest.TestCase):
    def scenario(self):
        sim = Simulation(seed=47, fleet_counts=dict(scouts=1, extinguishers=1, trucks=1))
        sim.ignite()
        sim.farmer_call()
        sim.step(2)
        self.assertFalse(sim.observation)
        return sim

    def test_smoke_hypotheses_do_not_become_sensor_observations(self):
        sim = self.scenario()
        signature = experience.signature(sim)
        self.assertGreater(signature['believed_fire_cells'], 0)
        self.assertEqual(signature['observed_fire_cells'], 0)
        self.assertFalse(signature['fire_confirmed'])
        self.assertIsNone(signature['fire_centroid'])
        self.assertIn('fire:unconfirmed', signature['labels'])
        brief = experience.brief(signature, [], [])
        self.assertIn('inferred from reports', brief['downwind_front'])
        self.assertFalse(reflex.describe(sim)['fire']['confirmed'])
        self.assertEqual(reflex.describe(sim)['fire']['seen_now_cells'], 0)

    def test_sensor_confirmation_counts_only_remembered_fire(self):
        sim = self.scenario()
        x, y = sim.fire_points()[0]
        sim.extinguishers[0].update(x=x, y=y)
        sim.observe()
        observed = [cell for cell in sim.memory.values() if cell['burning']]
        signature = experience.signature(sim)
        self.assertTrue(signature['fire_confirmed'])
        self.assertEqual(signature['observed_fire_cells'], len(observed))
        self.assertIn('fire:observed', signature['labels'])
        self.assertIn('sensor memory', experience.describe_front(signature))
        hidden = copy.deepcopy(sim)
        for row in hidden.cells:
            for cell in row:
                cell['heat'] = 0
        self.assertEqual(experience.signature(hidden)['observed_fire_cells'], len(observed))

    def test_satellite_inference_is_not_local_sensor_confirmation(self):
        sim = self.scenario()
        sim.satellite = dict(blocks=[[40, 32]])
        signature = experience.signature(sim)
        self.assertGreater(signature['believed_fire_cells'], 0)
        self.assertFalse(signature['fire_confirmed'])
        self.assertFalse(reflex.describe(sim)['fire']['confirmed'])


class WorkflowEnvironmentTests(unittest.TestCase):
    def test_selected_environment_reaches_trigger_without_changing_payload(self):
        payload = dict(event_type='farmer_call', world_state='{}')
        for configured, expected in ((None, 'development'), ('staging', 'staging'), ('production', 'production')):
            with self.subTest(environment=configured), patch.dict('os.environ', {}, clear=True):
                if configured:
                    environment = patch.dict('os.environ', {'HAPPYROBOT_ENVIRONMENT': configured})
                else:
                    environment = patch.dict('os.environ', {})
                with environment, patch.object(HappyRobot, 'tool', return_value={}) as tool:
                    with self.assertRaisesRegex(RuntimeError, 'did not complete'):
                        HappyRobot().decide(payload)
                self.assertEqual(tool.call_args.args[1]['environment'], expected)
                self.assertEqual(json.loads(tool.call_args.args[1]['payload']), payload)

    def test_invalid_environment_fails_before_starting_a_run(self):
        with patch.dict('os.environ', {'HAPPYROBOT_ENVIRONMENT': 'stagng'}), \
                patch.object(HappyRobot, 'tool') as tool:
            with self.assertRaisesRegex(ValueError, 'HAPPYROBOT_ENVIRONMENT'):
                HappyRobot().decide({})
            tool.assert_not_called()
