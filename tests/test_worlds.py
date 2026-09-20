import copy
import json
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from simulator.engine import Simulation
from simulator import worlds


def orders(sim, ext_command='hold', target=None):
    d = sim.extinguishers[0]
    tx, ty = target or (int(d['x']), int(d['y']))
    return dict(primary_command=ext_command, mission='m', drone_reason='r',
                extinguisher_orders=[dict(drone_id=d['drone_id'], command=ext_command, target_x=tx, target_y=ty, district_id='', reason='r')],
                scout_orders=[dict(drone_id=s['drone_id'], command='hold', waypoints=[], reason='r') for s in sim.scouts],
                truck_orders=[dict(truck_id=t['truck_id'], command='continue', reason='r') for t in sim.trucks])


def observing(seed=9, steps=30):
    """A drone sent to the smoke, so the agent has a real observation footprint around the fire."""
    s = Simulation(seed=seed, fleet_counts=dict(scouts=0, extinguishers=1, trucks=1))
    s.set_wind('north'); s.ignite(); s.farmer_call(); s.step(10)
    p = s.smoke_scout_positions()[0]
    s.apply(orders(s, 'scout', (p['x'], p['y'])), 'c1', s.incident_id, s.tick)
    s.step(steps)
    return s


def unseen_fuel(sim, count=3):
    """Fuel cells far from every vehicle that the agent has never observed."""
    far = [(x, y) for y, row in enumerate(sim.cells) for x, c in enumerate(row)
           if 2 <= x < sim.width-2 and 2 <= y < sim.height-2 and c['fuel'] > 0 and f'{x},{y}' not in sim.memory and all(abs(v['x']-x)+abs(v['y']-y) > 30 for v in sim.vehicles())]
    return far[:count]


def small(sim, **kw):
    return worlds.forecast(sim, horizon=8, branches=3, parallel=False, **kw)


REAL_FORECAST = worlds.forecast


def fast_forecast(sim, **kw):
    return REAL_FORECAST(sim, horizon=8, branches=2, parallel=False)


class BeliefWorldTests(unittest.TestCase):
    def test_belief_excludes_hidden_fire_and_keeps_observed(self):
        s = observing()
        hidden = unseen_fuel(s)[0]
        s.add_fire(*hidden)
        self.assertTrue(s.burning(s.cells[hidden[1]][hidden[0]]))
        b = worlds.belief_world(s)
        self.assertFalse(b.burning(b.cells[hidden[1]][hidden[0]]))
        remembered = {tuple(int(v) for v in k.split(',')) for k, m in s.memory.items() if m['burning']}
        self.assertTrue(remembered)
        self.assertEqual(set(map(tuple, b.fire_points())), remembered)
        self.assertEqual(s.tick, b.tick)
        self.assertEqual(b.history, [])

    def test_unobserved_smoke_report_seeds_the_belief(self):
        s = Simulation(); s.ignite(); s.farmer_call()
        b = worlds.belief_world(s)
        self.assertTrue(b.fire_points())
        self.assertTrue(all(abs(x-s.report[0]) <= 1 and abs(y-s.report[1]) <= 1 for x, y in b.fire_points()))


class DistanceTests(unittest.TestCase):
    def test_identity_zero_and_bounded(self):
        s = observing()
        v = worlds.world_vector(s)
        self.assertEqual(worlds.distance(v, v)['total'], 0.)
        other = copy.deepcopy(v)
        other['people'] = {k: 'burnt' for k in v['people']}
        other['fire'] = [(0, 0)]
        other['burned'] = [(79, 55)]
        d = worlds.distance(v, other)
        self.assertGreater(d['total'], 0.5)
        self.assertLessEqual(d['total'], 1.)
        self.assertEqual(d['terms']['people'], .5)  # every district unwarned -> burnt is two ranks of four

    def test_front_tolerance_forgives_one_cell_shift(self):
        a = [(10, 10), (11, 10), (12, 10)]
        b = [(x+1, y) for x, y in a]
        self.assertEqual(worlds._front_distance(a, b), 0.)
        self.assertEqual(worlds._front_distance(a, [(40, 40)]), 1.)
        self.assertEqual(worlds.unexpected_fire([(10, 10), (40, 40)], a), [(40, 40)])


class ForecastTests(unittest.TestCase):
    def test_deterministic_and_serialisable(self):
        s = observing()
        a, b = small(s), small(s)
        self.assertEqual(a['plans'], b['plans'])
        json.dumps(a)
        plan = a['plans']['current_orders']
        self.assertEqual(plan['branches'], 3)
        self.assertEqual(sorted(int(t) for t in plan['medoid']), [s.tick+4, s.tick+8])
        self.assertIn('farm', plan['districts'])
        self.assertEqual(s.tick, observing().tick)  # forecasting never advances the live world

    def test_branches_differ_and_dispersion_is_reported(self):
        s = observing()
        runs = [worlds.rollout(worlds.belief_world(s), None, 8, seed) for seed in (1, 2, 3)]
        finals = [r[max(r)] for r in runs]
        self.assertTrue(any(f['fire'] != finals[0]['fire'] for f in finals[1:]))
        summary = worlds.summarise(runs, worlds.world_vector(worlds.belief_world(s)))
        self.assertGreaterEqual(summary['dispersion'], 0.)
        self.assertTrue(all(0. <= p <= 1. for _, _, p in summary['burn_probability']))

    def test_invalid_plan_is_reported_not_scored(self):
        s = observing()
        bad = orders(s, 'contain', (65, 43))
        result = small(s, plans={'current_orders': None, 'bad': bad})
        self.assertFalse(result['plans']['bad']['valid'])
        self.assertTrue(result['plans']['bad']['error'])
        self.assertTrue(result['plans']['current_orders']['branches'])

    def test_agent_view_has_no_ground_truth_or_cell_dumps(self):
        s = observing()
        view = worlds.agent_view(small(s))
        text = json.dumps(view)
        self.assertNotIn('true_burning_cells', text)
        self.assertNotIn('medoid', text)
        self.assertIn('hidden fire excluded', view['basis'])
        self.assertLessEqual(len(view['likely_new_fire_cells']), 40)

    def test_parallel_matches_serial(self):
        s = observing()
        serial = worlds.forecast(s, horizon=4, branches=2, parallel=False)
        parallel = worlds.forecast(s, horizon=4, branches=2, parallel=True)
        self.assertEqual(serial['plans'], parallel['plans'])


class SurpriseTests(unittest.TestCase):
    def roll(self, s, forecast, steps, mutate=None):
        real = copy.deepcopy(s)
        if mutate:
            mutate(real)
        real.step(steps)
        return worlds.surprise(forecast, real), real

    def test_no_checkpoint_yet(self):
        s = observing()
        self.assertIsNone(worlds.surprise(small(s), s))

    def test_unchanged_world_is_not_divergent(self):
        s = observing()
        result, _ = self.roll(s, small(s), 8)
        self.assertFalse(result['divergent'])
        self.assertLessEqual(result['distance'], result['threshold'])
        self.assertEqual(result['compared_to_tick'], s.tick+8)

    def test_hidden_fire_outside_view_never_triggers_divergence(self):
        s = observing()
        f = small(s)
        result, real = self.roll(s, f, 8, lambda r: [r.add_fire(x, y) for x, y in unseen_fuel(r)])
        self.assertGreater(len(real.fire_points()), f['believed_burning_cells'])
        self.assertFalse(result['divergent'])
        self.assertFalse(any('beyond the forecast front' in c for c in result['what_changed']))

    def test_observed_new_fire_is_named_and_can_diverge(self):
        s = observing()
        f = small(s)
        d = s.extinguishers[0]
        seen = (int(d['x'])-6, int(d['y'])+6)
        result, _ = self.roll(s, f, 8, lambda r: [r.add_fire(seen[0]+dx, seen[1]+dy) for dx in range(3) for dy in range(3)])
        self.assertTrue(any('beyond the forecast front' in c for c in result['what_changed']), result['what_changed'])
        self.assertGreater(result['terms']['fire'], 0.)
        self.assertTrue(result['divergent'], result)

    def test_wind_change_is_recorded(self):
        s = observing()
        result, _ = self.roll(s, small(s), 4, lambda r: r.set_wind(x=-3, y=0))
        self.assertEqual(result['forecast_wind'], list(s.wind))
        self.assertEqual(result['wind_now'], [-3, 0])
        self.assertTrue(any(c.startswith('wind changed') for c in result['what_changed']))

    def test_wind_turn_breaks_the_premise_even_with_nothing_new_observed(self):
        s = observing()
        turned, _ = self.roll(s, small(s), 4, lambda r: r.set_wind(x=1, y=0))
        self.assertTrue(turned['premise_broken'])
        self.assertTrue(turned['divergent'])
        self.assertTrue(any('every branch assumed the old wind' in c for c in turned['what_changed']))
        nudged, _ = self.roll(s, small(s), 4, lambda r: r.set_wind(x=.3, y=-1))
        self.assertFalse(nudged['premise_broken'])
        self.assertTrue(any(c.startswith('wind changed') for c in nudged['what_changed']))

    def test_premise_check_needs_no_checkpoint(self):
        s = observing()
        f = small(s)
        self.assertIsNone(worlds.premise_check(f, s))
        s.set_wind(x=.3, y=-1)
        self.assertIsNone(worlds.premise_check(f, s))
        s.set_wind(x=1, y=0)
        result = worlds.premise_check(f, s)
        self.assertTrue(result['divergent'] and result['premise_broken'])
        self.assertIsNone(result['distance'])
        self.assertEqual(result['wind_now'], [1, 0])
        self.assertIn('every branch assumed the old wind', result['what_changed'][0])
        self.assertIsNone(worlds.premise_check(None, s))

    def test_wind_premise_rule(self):
        self.assertFalse(worlds.wind_premise_broken(None, (1, 0)))
        self.assertFalse(worlds.wind_premise_broken((0, -1), (0, -1)))
        self.assertTrue(worlds.wind_premise_broken((0, -1), (1, -1)))      # 45 degrees
        self.assertTrue(worlds.wind_premise_broken((0, -1), (0, -2)))      # strength +1
        self.assertTrue(worlds.wind_premise_broken((0, 0), (0, -1)))       # calm to wind
        self.assertFalse(worlds.wind_premise_broken((0, -1), (0, -1.5)))

    def test_threshold_is_bounded(self):
        s = observing()
        f = small(s)
        f['plans']['current_orders']['dispersion'] = 0.
        self.assertEqual(self.roll(s, f, 4)[0]['threshold'], worlds.DIVERGENCE_FLOOR)
        f['plans']['current_orders']['dispersion'] = 5.
        self.assertEqual(self.roll(s, f, 4)[0]['threshold'], worlds.DIVERGENCE_CEILING)


class ControllerForecastTests(unittest.TestCase):
    def _controller(self, tmp):
        from simulator.server import Controller
        with patch('simulator.server.RUNTIME', Path(tmp)):
            c = Controller()
        c.stop.set()
        c.sim.ignite(); c.sim.farmer_call()
        return c

    def test_decide_forecasts_before_and_after_and_records_both(self):
        with TemporaryDirectory() as tmp:
            c = self._controller(tmp)
            try:
                with patch.object(c.robot, 'decide', return_value=(orders(c.sim), 'e')) as decide, patch.object(c.analyst, 'analyse', return_value={}), \
                     patch.object(worlds, 'forecast', side_effect=fast_forecast):
                    c._decide(c.sim.payload('farmer_call'), c.sim.tick, copy.deepcopy(c.sim))
                    c.forecast_thread.join(30)
                state = json.loads(decide.call_args.args[0]['world_state'])
                self.assertEqual(state['possible_worlds']['issued_at'], 0)
                self.assertNotIn('true_burning_cells', decide.call_args.args[0]['world_state'])
                self.assertIsNotNone(c.forecast)
                self.assertIsNotNone(c.box.forecast(1, 'before'))
                self.assertIsNotNone(c.box.forecast(1, 'after'))
                view = c.state()['forecast']
                self.assertEqual(view['issued_at'], 0)
                self.assertNotIn('true_burning_cells', json.dumps(view))
                self.assertIn('Possible worlds', ' '.join(h['message'] for h in c.sim.history if h['source'] == 'forecast'))
                self.assertIsNotNone(c.postmortem()['decisions'][0]['forecast'])
            finally:
                c.robot.close(); c.box.close()

    def test_checkpoints_record_surprises_and_divergence_raises_event(self):
        with TemporaryDirectory() as tmp:
            c = self._controller(tmp)
            try:
                c.last_decision_id = c.box.record_decision(c.sim, c.sim.payload(), 'r', 1., orders(c.sim), 'applied')
                c.forecast = worlds.forecast(c.sim, horizon=8, branches=2, parallel=False)
                for _ in range(4):
                    c.sim.step(); c._check_forecast()
                self.assertEqual(len(c.surprises), 1)
                self.assertFalse(c.surprises[0]['divergent'])
                self.assertIsNone(c.sim.pending_decision_event)
                self.assertEqual(len(c.box.surprises(c.sim.incident_id)), 1)
                # Force a divergent reading at the next checkpoint.
                with patch.object(worlds, 'surprise', return_value=dict(tick=8, compared_to_tick=8, distance=.5, threshold=.05, divergent=True, terms={}, dispersion=0.,
                                                                       what_changed=['farm: fire within 8 cells (forecast 20, now 3)'], observed_cells=1,
                                                                       forecast_issued_at=0, forecast_wind=[0, -1], wind_now=[0, -1])):
                    for _ in range(4):
                        c.sim.step(); c._check_forecast()
                self.assertEqual(c.sim.pending_decision_event, 'forecast_divergence')
                self.assertTrue(c.forecast_consumed)
                self.assertEqual(c.sim.divergence['distance'], .5)
                self.assertEqual(json.loads(c.sim.payload('forecast_divergence')['world_state'])['forecast_divergence']['distance'], .5)
                self.assertTrue(any('Forecast divergence' in h['message'] for h in c.sim.history))
                self.assertEqual(sum(s['divergent'] for s in c.box.surprises(c.sim.incident_id)), 1)
                # Beyond the horizon nothing more is compared until a new forecast arrives.
                c.forecast_consumed = False
                for _ in range(8):
                    c.sim.step(); c._check_forecast()
                self.assertEqual(len(c.surprises), 2)
                self.assertEqual(len(c.state()['surprises']), 2)
            finally:
                c.robot.close(); c.box.close()

    def test_operator_wind_turn_names_the_broken_premise_before_the_forecast_update(self):
        with TemporaryDirectory() as tmp:
            c = self._controller(tmp)
            try:
                c.last_decision_id = c.box.record_decision(c.sim, c.sim.payload(), 'r', 1., orders(c.sim), 'applied')
                c.forecast = worlds.forecast(c.sim, horizon=8, branches=2, parallel=False)
                c.sim.step()
                with patch.object(c, 'request_decision') as request:
                    c.action('wind', dict(x=1, y=.3))
                    self.assertIsNone(c.sim.divergence)
                    self.assertFalse(c.forecast_consumed)
                    c.action('wind', dict(x=1, y=1))
                self.assertEqual([call.args[0] for call in request.call_args_list], ['forecast_update', 'forecast_update'])
                self.assertTrue(c.forecast_consumed)
                self.assertTrue(c.sim.divergence['premise_broken'])
                self.assertEqual(c.sim.divergence['wind_now'], [1, 1])
                self.assertIn('every branch assumed the old wind', json.loads(c.sim.payload('forecast_update')['world_state'])['forecast_divergence']['what_changed'][0])
                self.assertEqual(len(c.surprises), 1)
                self.assertEqual(sum(s['divergent'] for s in c.box.surprises(c.sim.incident_id)), 1)
                self.assertTrue(any('premise broken' in h['message'] for h in c.sim.history if h['source'] == 'forecast'))
                self.assertIsNone(c.state()['divergence']['distance'])
            finally:
                c.robot.close(); c.box.close()

    def test_corrective_retry_reuses_the_pre_decision_forecast(self):
        with TemporaryDirectory() as tmp:
            c = self._controller(tmp)
            try:
                payloads = []
                def decide(payload):
                    payloads.append(json.loads(payload['world_state']))
                    return (orders(c.sim, 'contain', (65, 43)) if len(payloads) == 1 else orders(c.sim)), 'e'
                with patch.object(c.robot, 'decide', side_effect=decide), patch.object(c.analyst, 'analyse', return_value={}), \
                     patch.object(worlds, 'forecast', side_effect=fast_forecast) as fc:
                    c.cursor = None
                    with c.lock:
                        c.request_decision()
                    for _ in range(200):
                        if len(payloads) == 2 and not c.busy:
                            break
                        time.sleep(.05)
                    c.forecast_thread.join(30)
                self.assertEqual(len(payloads), 2)
                self.assertEqual(payloads[0]['possible_worlds'], payloads[1]['possible_worlds'])
                self.assertEqual(fc.call_count, 2)  # one before, one after; the retry forecasts nothing
                self.assertIsNone(c.forecast_before)
            finally:
                c.robot.close(); c.box.close()

    def test_reset_clears_forecast_state(self):
        with TemporaryDirectory() as tmp:
            c = self._controller(tmp)
            try:
                c.forecast = dict(issued_at=0, horizon=8, plans={})
                c.surprises = [dict(tick=4)]
                c.action('reset', {})
                self.assertIsNone(c.forecast)
                self.assertEqual(c.surprises, [])
            finally:
                c.robot.close(); c.box.close()


if __name__ == '__main__':
    unittest.main()
