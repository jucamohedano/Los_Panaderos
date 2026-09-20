import copy
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from simulator import curator, worlds
from tests.test_experience import graded, scenario
from tests.test_worlds import orders, fast_forecast

RUN = '11111111-2222-4333-8444-555555555555'


class FakeRobot:
    """Answers trigger_run/monitor_runs the way the MCP proxy does, with the result node's data."""
    def __init__(self, data, status='completed'):
        self.data, self.status, self.calls = data, status, []
        self.connected = True

    def tool(self, name, arguments, timeout=60):
        self.calls.append((name, arguments))
        if name == 'trigger_run':
            return dict(content=[dict(text=f'Run ID: {RUN}\nStatus: {self.status}')])
        if arguments.get('output_id'):
            return dict(content=[dict(text='Data: ' + json.dumps(self.data))])
        return dict(content=[dict(text='listing')])

    @staticmethod
    def text(result):
        return '\n'.join(c['text'] for c in result['content'])

    @staticmethod
    def latest_output(listing):
        return 'out-1'


def result(**over):
    data = dict(mission='Avisar farm', priority_districts='["farm"]', downwind_front='norte', tactical_constraints='texto',
                source=json.dumps(dict(mission='agent', priority_districts='agent', downwind_front='deterministic', tactical_constraints='deterministic')),
                agent_used=True, confidence='0.8', applicable_cases='#1', set_aside='')
    data.update(over)
    return data


class CuratorClientTests(unittest.TestCase):
    def test_disabled_by_default_and_enabled_by_flag(self):
        with patch.dict('os.environ', {}, clear=True):
            self.assertFalse(curator.enabled())
        with patch.dict('os.environ', {curator.ENV_FLAG: '1'}):
            self.assertTrue(curator.enabled())

    def test_sends_only_the_workflow_contract_and_parses_the_result_node(self):
        robot = FakeRobot(result())
        payload = dict(event_id='e', event_type='farmer_call', incident_id='i', sim_time='3', world_state='{}',
                       mission='', drone_telemetry='secret', human_messages='x')
        out = curator.curate(robot, payload)
        sent = json.loads(robot.calls[0][1]['payload'])
        self.assertEqual(set(sent), set(curator.REQUEST))
        self.assertNotIn('drone_telemetry', sent)
        self.assertEqual(robot.calls[0][1]['workflow_id'], curator.WORKFLOW)
        self.assertEqual(robot.calls[1][1]['node_id'], curator.RESULT_NODE)
        self.assertEqual(out['fields'], dict(mission='Avisar farm', priority_districts='["farm"]', downwind_front='norte', tactical_constraints='texto'))
        self.assertTrue(out['agent_used']); self.assertEqual(out['confidence'], .8)
        self.assertEqual(out['source']['mission'], 'agent'); self.assertEqual(out['run_id'], RUN)

    def test_blank_fields_are_dropped_and_bad_confidence_is_zero(self):
        out = curator.curate(FakeRobot(result(mission='  ', confidence='n/a', source='not json')), dict(world_state='{}'))
        self.assertNotIn('mission', out['fields']); self.assertEqual(out['confidence'], 0.); self.assertEqual(out['source'], {})

    def test_incomplete_or_failed_runs_raise(self):
        with self.assertRaises(RuntimeError):
            curator.curate(FakeRobot(result(), status='failed'), dict(world_state='{}'))
        with self.assertRaises(RuntimeError):
            curator.curate(FakeRobot(dict(mission='only')), dict(world_state='{}'))


class ControllerCuratorTests(unittest.TestCase):
    def _controller(self, tmp):
        from simulator.server import Controller
        with patch('simulator.server.RUNTIME', Path(tmp)):
            c = Controller()
        c.stop.set()
        c.sim.ignite(); c.sim.farmer_call()
        return c

    def _decide(self, c, data, flag='1'):
        past = scenario(steps=0)
        graded(c.box, past, orders(past), 100.)
        robot, real = FakeRobot(data), curator.curate
        with patch.object(c.robot, 'decide', return_value=(orders(c.sim), 'e')) as decide, patch.object(c.analyst, 'analyse', return_value={}), \
             patch.object(worlds, 'forecast', side_effect=fast_forecast), patch.dict('os.environ', {curator.ENV_FLAG: flag}), \
             patch.object(curator, 'curate', side_effect=lambda r, p, timeout=90: real(robot, p)) as curate:
            c._decide(c.sim.payload('farmer_call'), c.sim.tick, copy.deepcopy(c.sim))
            c.forecast_thread.join(30)
        return decide.call_args.args[0], curate

    def test_off_by_default_keeps_the_deterministic_brief(self):
        with TemporaryDirectory() as tmp:
            c = self._controller(tmp)
            try:
                sent, curate = self._decide(c, result(), flag='')
                self.assertEqual(curate.call_count, 0)
                brief = json.loads(sent['world_state'])['episode_brief']
                self.assertEqual(sent['tactical_constraints'], brief['text'])
                self.assertNotIn('curator', c.experience)
            finally:
                c.robot.close(); c.box.close()

    def test_curated_fields_fill_the_mission_inputs_and_are_shown(self):
        with TemporaryDirectory() as tmp:
            c = self._controller(tmp)
            try:
                sent, _ = self._decide(c, result())
                self.assertEqual(sent['mission'], 'Avisar farm'); self.assertEqual(sent['priority_districts'], '["farm"]')
                self.assertEqual(sent['tactical_constraints'], 'texto')
                self.assertIn('episode_brief', json.loads(sent['world_state']))
                self.assertTrue(c.experience['curator']['agent_used'])
                self.assertEqual(c.state()['experience']['curator']['run_id'], RUN)
                self.assertTrue(any('Experience workflow applied' in h['message'] for h in c.sim.history if h['source'] == 'experience'))
            finally:
                c.robot.close(); c.box.close()

    def test_workflow_failure_falls_back_to_the_deterministic_brief(self):
        with TemporaryDirectory() as tmp:
            c = self._controller(tmp)
            try:
                sent, _ = self._decide(c, dict(mission='broken'))
                brief = json.loads(sent['world_state'])['episode_brief']
                self.assertEqual(sent['tactical_constraints'], brief['text'])
                self.assertNotIn('curator', c.experience)
                self.assertTrue(any('Experience workflow skipped' in h['message'] for h in c.sim.history if h['source'] == 'experience'))
            finally:
                c.robot.close(); c.box.close()


if __name__ == '__main__':
    unittest.main()
