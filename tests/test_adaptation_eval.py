import unittest

from simulator import adaptation_eval as ae, experience, oracle, worlds


class ScenarioFamilyTests(unittest.TestCase):
    def test_families_cover_every_axis_once_around_the_base(self):
        configs = ae.families()
        self.assertEqual(sum(c['family'] == 'base' for c in configs), 1)
        self.assertEqual({c['ignition'] for c in configs if c['family'] == 'ignition'}, set(ae.IGNITIONS)-{'default'})
        self.assertEqual({c['wind'] for c in configs if c['family'] == 'wind'}, set(ae.WINDS)-{'north'})
        self.assertEqual({c['fleet'] for c in configs if c['family'] == 'fleet'}, set(ae.FLEETS)-{'light'})
        self.assertTrue(all(c['shift'] for c in configs if c['family'] == 'wind_shift'))
        self.assertFalse(set(ae.TRAIN_SEEDS) & set(ae.TEST_SEEDS))

    def test_changed_ignition_moves_the_fire_towards_the_named_district(self):
        default, near = ae.build(23), ae.build(23, ignition='near_town_north')
        home = default.groups['town_north']['home']
        dist = lambda sim: min(abs(x-home[0])+abs(y-home[1]) for x, y in sim.fire_points())
        self.assertLess(dist(near), dist(default))
        self.assertEqual(ae.build(23, fleet='drones').scouts, [])
        self.assertEqual(list(ae.build(23, wind='west').wind), [-1, 0])


class BriefPolicyTests(unittest.TestCase):
    def test_brief_warns_the_exposed_district_first_and_is_valid(self):
        for wind, expected in (('north', 'farm'), ('west', 'town')):
            sim = ae.build(23, wind=wind)
            belief = worlds.belief_world(sim)
            plan = ae.brief_agent(belief, dict(event_type='local_observation'), None)
            self.assertTrue(oracle.is_valid(sim, plan), (wind, plan))
            warned = [o.get('district_id') for k in ('extinguisher_orders', 'scout_orders') for o in plan[k] if o['command'].startswith('evacuate')]
            self.assertEqual(warned[0], expected, (wind, warned))

    def test_only_exposed_districts_are_ranked(self):
        sig = experience.signature(ae.build(23, wind='north'))
        ranked = experience.rank_districts(sig)
        self.assertEqual([r['district_id'] for r in ranked], ['farm'])
        self.assertTrue(all(r['why'] for r in ranked))


class CalibrationTests(unittest.TestCase):
    def test_wind_shift_is_detected_and_stable_world_is_quiet(self):
        sim = ae.build(23)
        forecast = worlds.forecast(sim, horizon=8, branches=2, parallel=False)
        out = ae.calibration(sim, forecast, ae.SHIFTS['north'])
        self.assertFalse(out['stable']['divergent'], out['stable'])
        self.assertTrue(out['shifted']['divergent'], out['shifted'])
        self.assertTrue(any('wind changed' in c for c in out['shifted']['what_changed']))


if __name__ == '__main__':
    unittest.main()
