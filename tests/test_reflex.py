import copy
import json
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from simulator import server
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


REAL = {reflex.ENV_BACKEND: 'openrouter'}


class ReflexUnitTests(unittest.TestCase):
    def test_off_without_key_and_shadow_with_it(self):
        with patch.dict('os.environ', REAL, clear=True):
            self.assertEqual(reflex.mode(), 'off'); self.assertFalse(reflex.enabled())
        with patch.dict('os.environ', {**REAL, reflex.ENV_KEY: 'k'}):
            self.assertEqual(reflex.mode(), 'shadow'); self.assertTrue(reflex.enabled())
        with patch.dict('os.environ', {**REAL, reflex.ENV_KEY: 'k', reflex.ENV_MODE: 'off'}):
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

    @patch.dict('os.environ', REAL)
    def test_decide_assembles_a_valid_decision_and_routes_on_confidence_and_stakes(self):
        sim = scenario()
        options = oracle.vehicle_options(sim)
        with patch.object(reflex, 'ask', return_value=fake_answers(options, confidence=.95)):
            v = reflex.decide(sim, 'farmer_call', key='k')
        self.assertTrue(v['valid']); self.assertEqual(v['mode'], 'shadow')
        self.assertEqual(v['backend'], 'openrouter'); self.assertFalse(v['stub'])
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

    @patch.dict('os.environ', REAL)
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


class JevBackendTests(unittest.TestCase):
    """Offline: JEV_BACKEND selection, the stub's synthetic verdict, and its provenance in the black box."""

    def test_stub_is_the_default_and_openrouter_only_when_asked(self):
        for env in ({}, {reflex.ENV_BACKEND: ''}, {reflex.ENV_BACKEND: 'bogus'}, {reflex.ENV_KEY: 'k'}):
            with patch.dict('os.environ', env, clear=True):
                self.assertEqual(reflex.backend_name(), 'stub', env)
                self.assertIsInstance(reflex.backend(), reflex.StubBackend)
                self.assertEqual(reflex.mode(), 'shadow'); self.assertTrue(reflex.enabled())
        with patch.dict('os.environ', {reflex.ENV_BACKEND: 'OpenRouter'}, clear=True):
            self.assertEqual(reflex.backend_name(), 'openrouter')
            self.assertIsInstance(reflex.backend(), reflex.OpenRouterBackend)
            self.assertFalse(reflex.backend().available()); self.assertEqual(reflex.mode(), 'off')
            with self.assertRaises(RuntimeError):
                reflex.decide(scenario(), 'farmer_call')
        with patch.dict('os.environ', {reflex.ENV_BACKEND: 'openrouter', reflex.ENV_KEY: 'k'}, clear=True):
            self.assertTrue(reflex.backend().available())
        self.assertEqual(reflex.MODEL, 'typesafe/jev-1.13'); self.assertIn('openrouter.ai', reflex.URL)

    def test_openrouter_backend_posts_with_bearer_key(self):
        sent = {}
        def fake_post(body, key, timeout):
            sent.update(body=body, key=key, timeout=timeout)
            return dict(model=reflex.MODEL, answers={})
        with patch.object(reflex, '_post', side_effect=fake_post):
            raw = reflex.OpenRouterBackend().ask({'s': 1}, {'q': {}}, 'k', timeout=1.5)
        self.assertEqual(raw['model'], reflex.MODEL)
        self.assertEqual(sent['key'], 'k'); self.assertEqual(sent['timeout'], 1.5)
        self.assertEqual(sent['body'], dict(model=reflex.MODEL, state={'s': 1}, questions={'q': {}}))

    def test_openrouter_failures_fail_closed_with_unavailable_flag(self):
        import io
        import socket
        import urllib.error
        sim = scenario()
        http402 = urllib.error.HTTPError(reflex.URL, 402, 'Payment Required', {}, io.BytesIO(b'{"error":"no credits"}'))
        cases = [
            ('402', http402, 'HTTP 402 Payment Required'),
            ('timeout', socket.timeout('timed out'), 'TimeoutError: timed out'),
            ('url', urllib.error.URLError(ConnectionRefusedError('refused')), 'ConnectionRefusedError'),
            ('malformed-json', ValueError('Expecting value'), 'Expecting value'),
        ]
        for label, exc, cause in cases:
            with self.subTest(label), patch.dict('os.environ', REAL, clear=True), patch.object(reflex, '_post', side_effect=exc):
                v = reflex.decide(sim, 'farmer_call', key='k')   # must not raise
            self.assertEqual(v['backend'], 'openrouter'); self.assertFalse(v['stub']); self.assertTrue(v['unavailable'])
            self.assertIn(cause, v['error']); self.assertEqual(v['model'], reflex.MODEL)
            self.assertEqual(v['route'], 'central'); self.assertEqual(v['confidence'], 0.); self.assertEqual(v['escalate'], 1.)
            self.assertTrue(v['reasons'][0].startswith(reflex.UNAVAILABLE_REASON+': ')); self.assertIn(cause, v['reasons'][0])
            self.assertTrue(v['valid']); self.assertIn('UNAVAILABLE', v['decision']['mission']); self.assertTrue(v['orders'])
            json.dumps(v)
        # well-formed HTTP 200 but no answers object
        for body in (dict(model=reflex.MODEL), dict(model=reflex.MODEL, answers='nope'), ['not', 'a', 'dict']):
            with self.subTest(repr(body)), patch.dict('os.environ', REAL, clear=True), patch.object(reflex, '_post', return_value=body):
                v = reflex.decide(sim, 'farmer_call', key='k')
            self.assertTrue(v['unavailable']); self.assertEqual(v['route'], 'central'); self.assertIn('malformed', v['error'])
        # a healthy answer is not flagged
        options = oracle.vehicle_options(sim)
        with patch.dict('os.environ', REAL, clear=True), patch.object(reflex, '_post', return_value=fake_answers(options, confidence=.95)):
            ok = reflex.decide(sim, 'farmer_call', key='k')
        self.assertFalse(ok['unavailable']); self.assertIsNone(ok['error']); self.assertFalse(ok['stub'])
        # genuinely disabled still raises
        with patch.dict('os.environ', REAL, clear=True), self.assertRaises(RuntimeError):
            reflex.decide(sim, 'farmer_call')

    def test_unavailable_verdict_is_distinct_from_stub_in_the_black_box(self):
        import urllib.error
        from simulator.blackbox import BlackBox
        sim = scenario()
        exc = urllib.error.HTTPError(reflex.URL, 402, 'Payment Required', {}, None)
        with patch.dict('os.environ', REAL, clear=True), patch.object(reflex, '_post', side_effect=exc):
            v = reflex.decide(sim, 'farmer_call', key='k')
        with TemporaryDirectory() as tmp:
            box = BlackBox(Path(tmp)/'box.sqlite')
            did = box.record_decision(sim, dict(sim.payload(), event_type='farmer_call'), None, 0., dict(command='hold'), 'applied')
            box.save_reflex(did, v)
            rec = box.reflex(did)
            self.assertTrue(rec['unavailable']); self.assertFalse(rec['stub']); self.assertIn('402', rec['error'])
            summary = box.reflex_summary()
            self.assertEqual((summary['stub'], summary['unavailable'], summary['would_act']), (0, 1, 0))

    def test_stub_answers_are_well_formed_for_assemble(self):
        sim = scenario()
        options = oracle.vehicle_options(sim)
        qs, fixed = reflex.questions(options)
        raw = reflex.StubBackend().ask(reflex.describe(sim), qs)
        self.assertTrue(raw['stub']); self.assertEqual(raw['model'], reflex.STUB_MODEL)
        for vid, q in qs.items():
            if q['type'] == 'choice':
                self.assertIn(raw['answers'][vid]['choice'], q['criteria'])
                self.assertEqual(raw['answers'][vid]['confidence'], 0.)
        self.assertEqual(raw['answers']['escalate']['noul'], 1.)
        decision, confidence, unknown = reflex.assemble(raw['answers'], options, qs, fixed)
        self.assertEqual(unknown, []); self.assertEqual(confidence, 0.)
        self.assertTrue(oracle.is_valid(copy.deepcopy(sim), decision))

    def test_stub_decide_runs_end_to_end_offline_and_routes_to_central(self):
        sim = scenario()
        with patch.dict('os.environ', {}, clear=True), patch.object(reflex, '_post', side_effect=AssertionError('network')):
            v = reflex.decide(sim, 'farmer_call')
        self.assertEqual(v['backend'], 'stub'); self.assertTrue(v['stub']); self.assertEqual(v['model'], reflex.STUB_MODEL)
        self.assertNotEqual(v['model'], reflex.MODEL)
        self.assertEqual(v['route'], 'central'); self.assertEqual(v['confidence'], 0.); self.assertEqual(v['escalate'], 1.)
        self.assertEqual(v['reasons'][0], reflex.STUB_REASON)
        self.assertTrue(any('confidence' in r for r in v['reasons'])); self.assertTrue(any('Central' in r for r in v['reasons']))
        self.assertTrue(v['valid']); self.assertEqual(v['mode'], 'shadow'); self.assertIsNone(v['threat']['label'])
        self.assertIn('STUB', v['decision']['mission']); self.assertTrue(v['orders'])
        json.dumps(v)

    def test_black_box_record_keeps_stub_provenance(self):
        from simulator.blackbox import BlackBox
        sim = scenario()
        with patch.dict('os.environ', {}, clear=True):
            v = reflex.decide(sim, 'farmer_call')
        with TemporaryDirectory() as tmp:
            box = BlackBox(Path(tmp)/'box.sqlite')
            did = box.record_decision(sim, dict(sim.payload(), event_type='farmer_call'), None, 0., dict(command='hold'), 'applied')
            box.save_reflex(did, v, agreement=0.)
            rec = box.reflex(did)
            self.assertEqual(rec['route'], 'central'); self.assertEqual(rec['confidence'], 0.)
            self.assertEqual(rec['backend'], 'stub'); self.assertTrue(rec['stub'])
            self.assertEqual(rec['model'], reflex.STUB_MODEL)
            self.assertIn('stub', rec['reasons'][0])
            summary = box.reflex_summary()
            self.assertEqual(summary['decisions'], 1); self.assertEqual(summary['stub'], 1); self.assertEqual(summary['would_act'], 0)
            listed = box.list_decisions(sim.incident_id)[0]
            self.assertTrue(json.loads(listed['reflex_json'])['stub'])


@unittest.skipUnless(hasattr(server, 'RUNTIME'), 'legacy server.Controller (RUNTIME, box, robot, analyst) is gone in the new architecture; the adaptation stack is not wired into SimulatorSession yet')
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
