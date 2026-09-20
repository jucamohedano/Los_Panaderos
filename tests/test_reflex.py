import copy
import json
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from simulator import oracle, reflex, worlds
from simulator.engine import Simulation
from tests.test_worlds import orders, fast_forecast


def scenario():
    sim = Simulation(seed=9)
    sim.ignite(); sim.farmer_call(); sim.step(2)
    return sim


def fake_answers(options, choose=None, confidence=.95, escalate=.1, threat=1.2):
    """A Jev-shaped response that picks `choose[vehicle]` (label) or the first non-passive option per vehicle."""
    answers = {}
    for _, opts in options:
        vid = opts[0].get('drone_id') or opts[0].get('truck_id')
        labels = list(dict.fromkeys(reflex._label(o) for o in opts))
        if len(labels) < 2:
            continue
        pick = (choose or {}).get(vid) or labels[-1]
        rest = (1-confidence)/max(1, len(labels)-1)
        answers[vid] = dict(type='choice', choice=pick, confidence=confidence, probabilities={lab: (confidence if lab == pick else rest) for lab in labels})
    answers['escalate'] = dict(type='noul', noul=escalate)
    answers['threat'] = dict(type='score', score=threat)
    return dict(model='typesafe/jev-1.13-test', answers=answers)


class ReflexUnitTests(unittest.TestCase):
    def test_off_without_key_and_shadow_with_it(self):
        with patch.dict('os.environ', {}, clear=True):
            self.assertEqual(reflex.mode(), 'off'); self.assertFalse(reflex.enabled())
        with patch.dict('os.environ', {reflex.ENV_KEY: 'k'}):
            self.assertEqual(reflex.mode(), 'shadow'); self.assertTrue(reflex.enabled())
        with patch.dict('os.environ', {reflex.ENV_KEY: 'k', reflex.ENV_MODE: 'off'}):
            self.assertFalse(reflex.enabled())

    def test_state_is_compact_and_hides_the_truth(self):
        sim = scenario()
        state = reflex.describe(sim, 'farmer_call', brief=dict(priority_districts=['farm'], cases=[dict(did=['x'], regret=100., oracle_preferred=['y'])], lessons=[dict(rule='r')]))
        text = json.dumps(state)
        self.assertLess(len(text), 2500)
        self.assertNotIn('cells', state); self.assertNotIn('heat', text)
        self.assertEqual(state['priority_districts'], ['farm']); self.assertEqual(state['lessons'], ['r'])
        self.assertEqual(set(state['vehicles']), {v['drone_id'] for v in sim.extinguishers+sim.scouts} | {t['truck_id'] for t in sim.trucks})

    def test_questions_are_one_typed_choice_per_vehicle_with_real_candidates(self):
        sim = scenario()
        options = oracle.vehicle_options(sim)
        qs, fixed = reflex.questions(options)
        self.assertIn('escalate', qs); self.assertIn('threat', qs)
        self.assertEqual(qs['escalate']['type'], 'noul'); self.assertEqual(qs['threat']['type'], 'score')
        vehicles = [q for k, q in qs.items() if k not in ('escalate', 'threat')]
        self.assertTrue(vehicles)
        for q in vehicles:
            self.assertEqual(q['type'], 'choice'); self.assertGreaterEqual(len(q['criteria']), 2)
        # every label round-trips to a validated order
        _, _, unknown = reflex.assemble(fake_answers(options)['answers'], options, qs, fixed)
        self.assertEqual(unknown, [])

    def test_decide_assembles_a_valid_decision_and_routes_on_confidence_and_stakes(self):
        sim = scenario()
        options = oracle.vehicle_options(sim)
        with patch.object(reflex, 'ask', return_value=fake_answers(options, confidence=.95)):
            v = reflex.decide(sim, 'farmer_call', key='k')
        self.assertTrue(v['valid']); self.assertEqual(v['mode'], 'shadow')
        self.assertTrue(oracle.is_valid(copy.deepcopy(sim), v['decision']))
        self.assertEqual(v['threat']['label'], 'watch')
        self.assertGreaterEqual(v['confidence'], .9)
        # picking the last option means a high-stakes order for at least one vehicle here
        self.assertIn(v['route'], ('reflex', 'central'))
        with patch.object(reflex, 'ask', return_value=fake_answers(options, confidence=.3)):
            low = reflex.decide(sim, 'farmer_call', key='k')
        self.assertEqual(low['route'], 'central'); self.assertTrue(any('confidence' in r for r in low['reasons']))
        with patch.object(reflex, 'ask', return_value=fake_answers(options, escalate=.9)):
            esc = reflex.decide(sim, 'farmer_call', key='k')
        self.assertTrue(any('Central' in r for r in esc['reasons']))

    def test_unknown_choice_falls_back_and_escalates(self):
        sim = scenario()
        options = oracle.vehicle_options(sim)
        answers = fake_answers(options)
        for k, a in answers['answers'].items():
            if a['type'] == 'choice':
                a['choice'] = 'fly to the moon'
        with patch.object(reflex, 'ask', return_value=answers):
            v = reflex.decide(sim, 'farmer_call', key='k')
        self.assertEqual(v['route'], 'central'); self.assertTrue(v['valid'])
        self.assertTrue(any('unknown choice' in r for r in v['reasons']))

    def test_command_confidence_pools_equivalent_targets(self):
        lookup = {'scout 1,1': dict(command='scout'), 'scout 2,2': dict(command='scout'), 'hold': dict(command='hold')}
        ans = dict(choice='scout 1,1', confidence=.4, probabilities={'scout 1,1': .4, 'scout 2,2': .4, 'hold': .2})
        self.assertAlmostEqual(reflex._command_confidence(ans, lookup, 'scout 1,1'), .8)

    def test_agreement_and_grade(self):
        sim = scenario()
        hold = orders(sim)
        self.assertEqual(reflex.agreement(hold, hold), 1.)
        self.assertEqual(reflex.agreement(hold, {}), 0.)
        evaluation = oracle.evaluate(copy.deepcopy(sim), hold, horizon=4, seeds=(9,), max_joint=2, time_budget=5)
        grade = reflex.grade(sim, dict(valid=True, decision=hold), evaluation)
        self.assertEqual(grade['cost'], evaluation['actual_cost']); self.assertEqual(grade['regret'], evaluation['regret'])
        self.assertIsNone(reflex.grade(sim, dict(valid=False, decision=hold), evaluation))


class ControllerReflexTests(unittest.TestCase):
    def _controller(self, tmp):
        from simulator.server import Controller
        with patch('simulator.server.RUNTIME', Path(tmp)):
            c = Controller()
        c.stop.set()
        c.sim.ignite(); c.sim.farmer_call(); c.sim.step(2)
        return c

    def test_shadow_verdict_is_recorded_graded_and_never_applied(self):
        with TemporaryDirectory() as tmp:
            c = self._controller(tmp)
            try:
                options = oracle.vehicle_options(c.sim)
                actual, evaluate = orders(c.sim), oracle.evaluate
                quick = lambda snap, dec, sig=None, **kw: evaluate(snap, dec, sig, horizon=4, seeds=(9,), max_joint=2, time_budget=5)
                with patch.object(c.robot, 'decide', return_value=(actual, 'e')), patch.object(worlds, 'forecast', side_effect=fast_forecast), \
                     patch.dict('os.environ', {reflex.ENV_KEY: 'k'}), patch.object(reflex, 'ask', return_value=fake_answers(options)), \
                     patch('simulator.analysis.telemetry.harvest', return_value=[]), patch('simulator.analysis.reflection.reflect', side_effect=RuntimeError('offline')), \
                     patch.object(oracle, 'evaluate', side_effect=quick):
                    c._decide(c.sim.payload('farmer_call'), c.sim.tick, copy.deepcopy(c.sim))
                    c.forecast_thread.join(30)
                    for _ in range(100):
                        if c.box.reflex(1) and c.box.reflex(1).get('grade') is not None:
                            break
                        time.sleep(.1)
                    view = c.state()['reflex']
                self.assertNotIn('jev', json.dumps(c.sim.history).lower().replace('jev shadow', ''))
                self.assertEqual(view['mode'], 'shadow'); self.assertEqual(view['decision_id'], 1)
                self.assertIn(view['route'], ('reflex', 'central')); self.assertIsNotNone(view['agreement'])
                stored = c.box.reflex(1)
                self.assertIn('grade', stored); self.assertIn('regret', stored['grade'])
                pm = c.postmortem()['decisions'][0]['reflex']
                self.assertNotIn('decision', pm); self.assertEqual(pm['route'], view['route'])
                summary = c.learning()['reflex']
                self.assertEqual(summary['decisions'], 1); self.assertEqual(summary['graded'], 1)
                self.assertTrue(any(h['source'] == 'reflex' for h in c.sim.history))
            finally:
                c.robot.close(); c.box.close()

    def test_reflex_failure_never_blocks_the_decision(self):
        with TemporaryDirectory() as tmp:
            c = self._controller(tmp)
            try:
                with patch.object(c.robot, 'decide', return_value=(orders(c.sim), 'e')), patch.object(worlds, 'forecast', side_effect=fast_forecast), \
                     patch.dict('os.environ', {reflex.ENV_KEY: 'k'}), patch.object(reflex, 'ask', side_effect=OSError('timeout')), \
                     patch.object(c.analyst, 'analyse', return_value={}):
                    c._decide(c.sim.payload('farmer_call'), c.sim.tick, copy.deepcopy(c.sim))
                    c.forecast_thread.join(30)
                    self.assertEqual(c.state()['reflex'], dict(mode='shadow'))
                self.assertIsNone(c.error)
                self.assertIsNone(c.box.reflex(1))
                self.assertTrue(any('Jev shadow skipped' in h['message'] for h in c.sim.history))
            finally:
                c.robot.close(); c.box.close()

    def test_off_by_default(self):
        with TemporaryDirectory() as tmp:
            c = self._controller(tmp)
            try:
                with patch.object(c.robot, 'decide', return_value=(orders(c.sim), 'e')), patch.object(worlds, 'forecast', side_effect=fast_forecast), \
                     patch.dict('os.environ', {}, clear=True), patch.object(reflex, 'ask') as ask, patch.object(c.analyst, 'analyse', return_value={}):
                    c._decide(c.sim.payload('farmer_call'), c.sim.tick, copy.deepcopy(c.sim))
                    c.forecast_thread.join(30)
                    self.assertEqual(ask.call_count, 0); self.assertEqual(c.state()['reflex'], dict(mode='off'))
            finally:
                c.robot.close(); c.box.close()


if __name__ == '__main__':
    unittest.main()
