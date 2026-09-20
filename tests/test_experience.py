import copy
import json
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from simulator import episodes, experience, worlds
from simulator.blackbox import BlackBox
from simulator.engine import Simulation
from simulator.healing import Healer
from tests.test_worlds import orders, fast_forecast, unseen_fuel


def scenario(seed=9, wind='north', steps=2):
    s = Simulation(seed=seed, fleet_counts=dict(scouts=1, extinguishers=1, trucks=1))
    s.set_wind(wind); s.ignite(); s.farmer_call(); s.step(steps)
    return s


def graded(box, sim, decision, regret, best=None, event='farmer_call', diagnosis=None, status='applied'):
    """A finished, oracle-graded past decision in the black box."""
    did = box.record_decision(sim, sim.payload(event), None, 1., decision, status)
    box.save_case(did, sim.incident_id, experience.signature(sim, event))
    box.save_evaluation(did, dict(regret=regret, gap_type='judgement' if regret else 'none', best_decision=best or decision, actual_cost=regret, best_cost=0.))
    if diagnosis:
        box.save_reflection(did, None, 'text', diagnosis)
    box.finish_outcome(did, sim)
    return did


class SignatureTests(unittest.TestCase):
    def test_signature_is_deterministic_and_belief_only(self):
        s = scenario()
        a, b = experience.signature(s), experience.signature(copy.deepcopy(s))
        self.assertEqual(a, b)
        for x, y in unseen_fuel(s):
            s.cells[y][x].update(heat=.6, age=0)
        s.update_people_exposure()
        self.assertEqual(experience.signature(s)['believed_fire_cells'], a['believed_fire_cells'])
        self.assertNotIn('true_burning_cells', json.dumps(a))
        self.assertEqual(set(a['districts']), set(s.groups))
        self.assertIn('idle_scouts', a['fleet'])

    def test_distance_is_zero_for_same_situation_and_grows_with_material_change(self):
        s = scenario()
        base = experience.signature(s)
        self.assertEqual(experience.signature_distance(base, base)['total'], 0.)
        windy = copy.deepcopy(s); windy.set_wind('south')
        d_wind = experience.signature_distance(base, experience.signature(windy))
        self.assertGreater(d_wind['terms']['wind'], .3)
        warned = copy.deepcopy(s)
        for g in warned.groups.values():
            g['status'] = 'safe'
        d_people = experience.signature_distance(base, experience.signature(warned))
        self.assertGreater(d_people['terms']['districts'], 0.)
        self.assertGreater(d_people['total'], d_people['terms']['districts']*experience.SIGNATURE_WEIGHTS['districts']-1e-9)
        self.assertLessEqual(max(d_wind['total'], d_people['total']), 1.)

    def test_far_districts_downwind_are_still_downwind(self):
        s = scenario(wind='west')
        sig = experience.signature(s)
        far = [k for k, d in sig['districts'].items() if d['distance'] >= experience.FAR_CELLS]
        self.assertTrue(far)
        self.assertTrue(any(sig['districts'][k]['downwind'] >= .5 for k in far), sig['districts'])
        self.assertIn('people:downwind', sig['labels'])
        self.assertEqual(sig['label_version'], 2)

    def test_fleet_composition_separates_otherwise_identical_situations(self):
        light = Simulation(seed=9, fleet_counts=dict(scouts=1, extinguishers=1, trucks=0))
        truck = Simulation(seed=9, fleet_counts=dict(scouts=1, extinguishers=1, trucks=1))
        for s in (light, truck):
            s.set_wind('north'); s.ignite(); s.farmer_call(); s.step(2)
        d = experience.signature_distance(experience.signature(light), experience.signature(truck))
        self.assertGreater(d['terms']['fleet'], 0.)
        self.assertLess(d['total'], experience.MAX_CASE_DISTANCE)

    def test_first_minutes_phase_matters_more_than_late_ticks(self):
        early, mid = scenario(steps=2), scenario(steps=12)
        late, later = scenario(steps=40), scenario(steps=50)
        d_early = experience.signature_distance(experience.signature(early), experience.signature(mid))['terms']['phase']
        d_late = experience.signature_distance(experience.signature(late), experience.signature(later))['terms']['phase']
        self.assertGreater(d_early, 0.)
        self.assertEqual(d_late, 0.)


class RetrievalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.box = BlackBox(Path(self.tmp.name)/'bb.sqlite')

    def tearDown(self):
        self.box.close(); self.tmp.cleanup()

    def test_empty_memory_returns_no_cases(self):
        s = scenario()
        self.assertEqual(experience.retrieve(self.box, experience.signature(s)), [])
        self.assertEqual(experience.relevant_lessons(self.box, experience.signature(s)), [])

    def test_nearest_graded_case_first_and_own_incident_excluded(self):
        past = scenario()
        best = orders(past, 'scout', tuple(past.smoke_scout_positions()[0].values())[:2]) if past.smoke_scout_positions() else orders(past)
        near = graded(self.box, past, orders(past), 100., best, diagnosis=dict(root_cause='held', proposed_rule='Send the scout to the smoke first.'))
        far_sim = scenario(wind='south', steps=25)
        for g in far_sim.groups.values():
            g['status'] = 'safe'
        graded(self.box, far_sim, orders(far_sim), 0.)
        now = scenario()
        cases = experience.retrieve(self.box, experience.signature(now), exclude_incident=now.incident_id)
        self.assertEqual([c['decision_id'] for c in cases], [near])  # the far, all-safe, south-wind case is not shown
        self.assertEqual(cases[0]['similarity_distance'], 0.)
        self.assertEqual(cases[0]['regret'], 100.)
        self.assertEqual(cases[0]['lesson'], 'Send the scout to the smoke first.')
        self.assertTrue(cases[0]['did'])
        self.assertTrue(cases[0]['oracle_preferred'])
        self.assertTrue(all(c['similarity_distance'] <= experience.MAX_CASE_DISTANCE for c in cases))
        dumped = json.dumps(cases)
        for secret in ('snapshot', 'true_burning', 'payload_json', 'Authorization'):
            self.assertNotIn(secret, dumped)
        self.assertEqual(experience.retrieve(self.box, experience.signature(past), exclude_incident=past.incident_id), [])

    def test_ungraded_rejected_or_signatureless_decisions_are_skipped(self):
        s = scenario()
        self.box.record_decision(s, s.payload(), None, 1., orders(s), 'applied')  # no case, no evaluation
        rid = self.box.record_decision(s, s.payload(), None, 1., orders(s), 'rejected')
        self.box.save_case(rid, s.incident_id, experience.signature(s)); self.box.save_evaluation(rid, dict(regret=5.))
        self.assertEqual(experience.retrieve(self.box, experience.signature(s)), [])

    def test_lessons_are_ranked_by_situation_relevance(self):
        s = scenario()
        far = scenario(wind='south', steps=25)
        self.box.add_lesson('Far rule', None, context=experience.signature(far), confidence=.9)
        self.box.add_lesson('Near rule', None, context=experience.signature(s), confidence=.9)
        self.box.add_lesson('Context-free rule', None)
        ranked = experience.relevant_lessons(self.box, experience.signature(s), limit=2)
        self.assertEqual([l['rule'] for l in ranked], ['Near rule', 'Far rule'])
        self.assertEqual(ranked[0]['relevance'], 1.)
        self.assertEqual(self.box.active_lessons(5)[0], 'Context-free rule')  # recency order still available


class LessonCreditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.box = BlackBox(Path(self.tmp.name)/'bb.sqlite')
        self.sim = scenario()

    def tearDown(self):
        self.box.close(); self.tmp.cleanup()

    def _decision(self, regret, lesson_ids=()):
        did = self.box.record_decision(self.sim, self.sim.payload(), None, 1., orders(self.sim), 'applied')
        self.box.record_lesson_uses(did, list(lesson_ids))
        self.box.save_evaluation(did, dict(regret=regret, gap_type='none'))
        return did

    def test_episodes_average_only_measured_surprises(self):
        did = self._decision(0.)
        self.box.save_surprise(did, worlds.premise_check(dict(wind=[1, 0], plans=dict(current_orders=dict(dispersion=.01))), self.sim))
        ep = self.box.episodes()[0]
        self.assertEqual((ep['surprise_checks'], ep['divergences'], ep['mean_surprise']), (1, 1, None))
        self.box.save_surprise(did, dict(tick=4, distance=.2, threshold=.05, divergent=True, what_changed=[]))
        ep = self.box.episodes()[0]
        self.assertEqual((ep['surprise_checks'], ep['divergences'], ep['mean_surprise']), (2, 2, .2))

    def test_credit_compares_shown_versus_not_shown_and_retires_harmful_lessons(self):
        good = self.box.add_lesson('Good rule', None, confidence=.8)
        bad = self.box.add_lesson('Bad rule', None, confidence=.8)
        time.sleep(.01)
        for _ in range(3):
            self._decision(0., [good]); self._decision(150., [bad]); self._decision(50.)
        self.assertEqual(experience.credit(self.box, good), dict(uses=3, regret_with=0., regret_without=100.))
        self.assertEqual(experience.credit(self.box, bad), dict(uses=3, regret_with=150., regret_without=25.))
        self.assertEqual(experience.review_lessons(self.box), [bad])
        rows = {r['id']: r for r in self.box.lesson_rows(active_only=False)}
        self.assertEqual(rows[bad]['active'], 0); self.assertIn('regret with lesson 150.0', rows[bad]['retired_reason'])
        self.assertEqual(rows[good]['active'], 1); self.assertEqual(rows[good]['regret_with'], 0.)
        self.assertEqual(self.box.active_lessons(), ['Good rule'])
        self.box.restore_lesson(bad)
        self.assertEqual(len(self.box.active_lessons()), 2)

    def test_too_few_uses_never_retires(self):
        lid = self.box.add_lesson('Young rule', None, confidence=.8)
        self._decision(500., [lid]); self._decision(0.)
        self.assertEqual(experience.review_lessons(self.box), [])

    def test_add_lesson_dedupes_and_keeps_context(self):
        sig = experience.signature(self.sim)
        a = self.box.add_lesson('Same rule', 1, context=sig, gap_type='judgement', confidence=.6)
        b = self.box.add_lesson('same  RULE', 2, confidence=.9)
        self.assertEqual(a, b)
        row = self.box.lesson_rows()[0]
        self.assertEqual(row['confidence'], .9); self.assertEqual(json.loads(row['context_json']), sig)
        self.assertIsNone(self.box.add_lesson('   ', None))

    def test_tier1_gates_on_confidence_and_stores_context(self):
        did = self.box.record_decision(self.sim, self.sim.payload(), None, 1., orders(self.sim), 'applied')
        self.box.save_case(did, self.sim.incident_id, experience.signature(self.sim))
        healer = Healer(self.box, None, 'wf', self.tmp.name)
        weak = healer.tier1(did, None, dict(gap_type='judgement', proposed_rule='Weak rule', confidence=.3), dict(regret=100.))
        self.assertIsNone(weak['lesson']); self.assertTrue(weak['lesson_gated_out'])
        strong = healer.tier1(did, None, dict(gap_type='judgement', proposed_rule='Strong rule', confidence=.8), dict(regret=100.))
        self.assertEqual(strong['lesson'], 'Strong rule')
        row = self.box.lesson_rows()[0]
        self.assertEqual(row['gap_type'], 'judgement'); self.assertIsNotNone(row['context_json'])


class ControllerExperienceTests(unittest.TestCase):
    def _controller(self, tmp):
        from simulator.server import Controller
        with patch('simulator.server.RUNTIME', Path(tmp)):
            c = Controller()
        c.stop.set()
        c.sim.ignite(); c.sim.farmer_call()
        return c

    def test_payload_carries_similar_cases_and_relevant_lessons_and_records_uses(self):
        with TemporaryDirectory() as tmp:
            c = self._controller(tmp)
            try:
                past = scenario(steps=0)
                graded(c.box, past, orders(past), 100., diagnosis=dict(root_cause='held', proposed_rule='Warn the farm first.'))
                lid = c.box.add_lesson('Warn the farm first.', None, context=experience.signature(past), confidence=.9)
                with patch.object(c.robot, 'decide', return_value=(orders(c.sim), 'e')) as decide, patch.object(c.analyst, 'analyse', return_value={}), \
                     patch.object(worlds, 'forecast', side_effect=fast_forecast):
                    c._decide(c.sim.payload('farmer_call'), c.sim.tick, copy.deepcopy(c.sim))
                    c.forecast_thread.join(30)
                state = json.loads(decide.call_args.args[0]['world_state'])
                self.assertEqual(len(state['similar_cases']), 1)
                self.assertEqual(state['similar_cases'][0]['regret'], 100.)
                self.assertEqual(state['lessons_learned'], ['Warn the farm first.'])
                sent = decide.call_args.args[0]
                brief = state['episode_brief']
                self.assertEqual(brief['cases'][0]['decision_id'], 1)
                self.assertEqual(brief['lessons'][0]['rule'], 'Warn the farm first.')
                self.assertEqual(brief['forecast']['horizon'], state['possible_worlds']['horizon'])
                self.assertEqual(sent['tactical_constraints'], brief['text'])
                self.assertEqual(set(json.loads(sent['priority_districts'])), set(brief['situation']['unwarned']) & set(json.loads(sent['priority_districts'])))
                self.assertIn('mission', sent); self.assertIn('downwind_front', sent)
                self.assertNotIn('true_burning_cells', decide.call_args.args[0]['world_state'])
                self.assertIsNotNone(c.box.case_signature(2))
                self.assertEqual(c.box.lesson_regrets(lid)[0], [])  # shown but not graded yet
                self.assertEqual(c.box.lesson_rows()[0]['uses'], 1)
                self.assertEqual(c.state()['experience']['decision_id'], 2)
                learning = c.learning()
                self.assertEqual(learning['experience']['cases'][0]['decision_id'], 1)
                self.assertEqual(len(learning['episodes']), 2)
                self.assertEqual(learning['episodes'][0]['mean_regret'], 100.)
                self.assertEqual(learning['episodes'][1]['cases_available'], 1)
                self.assertTrue(any('similar past decision' in h['message'] for h in c.sim.history if h['source'] == 'experience'))
            finally:
                c.robot.close(); c.box.close()

    def test_lesson_action_retires_and_restores(self):
        with TemporaryDirectory() as tmp:
            c = self._controller(tmp)
            try:
                lid = c.box.add_lesson('Rule', None, confidence=.9)
                c.action('lesson', dict(id=lid, active=False))
                self.assertEqual(c.sim.lessons, [])
                self.assertEqual(c.box.lesson_rows(active_only=False)[0]['retired_reason'], 'retired by operator')
                c.action('lesson', dict(id=lid, active=True))
                self.assertEqual(c.sim.lessons, ['Rule'])
            finally:
                c.robot.close(); c.box.close()

    def test_reset_clears_shown_experience(self):
        with TemporaryDirectory() as tmp:
            c = self._controller(tmp)
            try:
                c.experience = dict(cases=[])
                c.action('reset', {})
                self.assertIsNone(c.experience)
            finally:
                c.robot.close(); c.box.close()


class EpisodeHarnessTests(unittest.TestCase):
    def test_experience_agent_adopts_the_oracle_plan_of_a_close_case(self):
        with TemporaryDirectory() as tmp:
            box = BlackBox(Path(tmp)/'ep.sqlite')
            try:
                past = scenario()
                best = orders(past, 'scout', (past.report[0]-3, past.report[1]))
                graded(box, past, orders(past), 100., best)
                now = scenario()
                sig = experience.signature(now)
                state = dict(similar_cases=experience.retrieve(box, sig, exclude_incident=now.incident_id))
                plan = episodes.experience_agent(now, state, box)
                self.assertIn('from case', plan['mission'])
                self.assertEqual(plan['extinguisher_orders'][0]['command'], 'scout')
                self.assertEqual(episodes.experience_agent(now, dict(similar_cases=[]), box)['mission'], 'baseline')
                self.assertEqual(episodes.hold_agent(now, {}, box)['mission'], 'baseline')
            finally:
                box.close()

    def test_experience_agent_also_adopts_a_zero_regret_case(self):
        with TemporaryDirectory() as tmp:
            box = BlackBox(Path(tmp)/'ep.sqlite')
            try:
                past = scenario()
                good = orders(past, 'scout', (past.report[0]-3, past.report[1]))
                graded(box, past, good, 0.)
                now = scenario()
                state = dict(similar_cases=experience.retrieve(box, experience.signature(now), exclude_incident=now.incident_id))
                self.assertIsNone(state['similar_cases'][0]['oracle_preferred'])
                plan = episodes.experience_agent(now, state, box)
                self.assertIn('regret 0', plan['mission'])
                self.assertEqual(plan['extinguisher_orders'][0]['command'], 'scout')
            finally:
                box.close()

    def test_experience_agent_regrounds_intents_on_a_different_fleet(self):
        with TemporaryDirectory() as tmp:
            box = BlackBox(Path(tmp)/'ep.sqlite')
            try:
                past = scenario()
                farm = past.groups['farm']['home']
                best = orders(past, 'evacuate_farm', farm)
                best['extinguisher_orders'][0]['district_id'] = 'farm'
                graded(box, past, orders(past), 100., best)
                now = Simulation(seed=9, fleet_counts=dict(scouts=0, extinguishers=2, trucks=0))
                now.set_wind('north'); now.ignite(); now.farmer_call(); now.step(2)
                self.assertFalse(episodes.oracle.is_valid(now, best))
                state = dict(similar_cases=experience.retrieve(box, experience.signature(now), exclude_incident=now.incident_id))
                self.assertTrue(state['similar_cases'])
                plan = episodes.experience_agent(now, state, box)
                self.assertIn('re-grounded', plan['mission'])
                self.assertEqual({o['drone_id'] for o in plan['extinguisher_orders']}, {d['drone_id'] for d in now.extinguishers})
                self.assertEqual([o['command'] for o in plan['extinguisher_orders']].count('evacuate_farm'), 1)
                self.assertEqual(plan['scout_orders'], [])
                self.assertTrue(episodes.oracle.is_valid(now, plan))
                self.assertIsNone(episodes.reground(now, episodes.hold_orders(past)))
            finally:
                box.close()

    def test_run_records_graded_episodes_deterministically(self):
        seen = []
        def fake_evaluate(snapshot, actual, signals=None, **kw):
            seen.append(kw)
            return dict(regret=100. if actual.get('mission') == 'baseline' else 0., gap_type='none', best_decision=orders(snapshot, 'scout', (snapshot.report[0]-3, snapshot.report[1])))
        with TemporaryDirectory() as tmp, patch.object(episodes.oracle, 'evaluate', side_effect=fake_evaluate):
            result = episodes.run(Path(tmp)/'ep.sqlite', episodes=2, decisions=2, log=lambda *a: None)
        self.assertEqual([r['regrets'] for r in result['runs']], [[100., 100.], [0., 0.]])
        curve = result['curve']
        self.assertEqual([e['mean_regret'] for e in curve], [100., 0.])
        self.assertEqual([e['cases_available'] for e in curve], [0, 2])
        self.assertEqual(seen[0]['horizon'], 8)


if __name__ == '__main__':
    unittest.main()
