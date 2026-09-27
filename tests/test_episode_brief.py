"""The decision-time brief: bounded, evidence-only, and fills the fleet workflow's mission inputs."""
import json
import unittest

from simulator import experience


def sig(**over):
    base = dict(tick=3, event_type='farmer_call', wind=[0, -2], wind_strength=2., believed_fire_cells=4, fire_confirmed=True,
                fire_centroid=[40., 30.], fleet=dict(scouts=1, extinguishers=1, trucks=1, idle_scouts=1, idle_extinguishers=1, trucks_mobile=0),
                districts=dict(farm=dict(status='unwarned', distance=9., downwind=.9, people=100),
                               town=dict(status='unwarned', distance=40., downwind=0., people=4000),
                               hamlet=dict(status='warned', distance=6., downwind=.8, people=50)))
    base.update(over)
    return dict(base, label_version=experience.LABEL_VERSION, labels=experience.situation_labels(base))


def case(**over):
    base = dict(decision_id=7, incident_id='past', similarity_distance=.08, matching_labels=['wind:north', 'fire:observed'],
                differing_labels=dict(current_only=['people:nearby'], case_only=[]), did=['drone-1: hold', 'scout-1: hold'],
                regret=120., gap_type='judgement', oracle_preferred=['drone-1: evacuate_farm farm'],
                root_cause='x'*500, lesson='Warn downwind districts before scouting.')
    base.update(over)
    return base


class Brief(unittest.TestCase):
    def test_priority_ranks_exposed_unwarned_only(self):
        b = experience.brief(sig(), [], [])
        self.assertEqual([r['district_id'] for r in b['priority_districts']], ['farm'])
        self.assertEqual(b['situation']['unwarned'], ['farm', 'town'])
        self.assertIn('downwind', b['priority_districts'][0]['why'][0])

    def test_forecast_threat_raises_priority_and_is_explained(self):
        view = dict(horizon=16, branches=8, dispersion=.1, expected_burning_cells=20,
                    districts=dict(town=dict(p_fire_within_8=.75), farm=dict(p_fire_within_8=0.)))
        b = experience.brief(sig(), [], [], view)
        self.assertEqual(b['priority_districts'][0]['district_id'], 'town')
        self.assertIn('75% of possible worlds', b['priority_districts'][0]['why'][0])
        self.assertEqual(b['forecast']['threatened'], ['town'])

    def test_cases_are_compact_and_clipped(self):
        b = experience.brief(sig(), [case(decision_id=i) for i in range(6)], [dict(id=1, rule='r'*400, relevance=.9)])
        self.assertEqual(len(b['cases']), experience.BRIEF_CASES)
        c = b['cases'][0]
        self.assertLessEqual(len(c['root_cause']), experience.BRIEF_FIELD_CHARS)
        self.assertEqual(c['differs'], ['people:nearby'])
        self.assertEqual(c['oracle_preferred'], ['drone-1: evacuate_farm farm'])
        self.assertLessEqual(len(b['lessons'][0]['rule']), experience.BRIEF_FIELD_CHARS)
        self.assertLessEqual(len(b['text']), experience.BRIEF_TEXT_CHARS)
        self.assertIn('evidence, not orders', b['text'])
        for banned in ('result_json', 'snapshot', 'true_burning', 'hidden'):
            self.assertNotIn(banned, json.dumps(b))

    def test_divergence_is_named_only_when_divergent(self):
        held = experience.brief(sig(), [], [], None, dict(divergent=False, what_changed=['x']))
        self.assertIsNone(held['divergence'])
        broke = experience.brief(sig(), [], [], None, dict(divergent=True, distance=.3, threshold=.1, what_changed=['wind changed']))
        self.assertEqual(broke['divergence']['what_changed'], ['wind changed'])
        self.assertIn('Forecast broke: wind changed', broke['text'])

    def test_dispatch_fields_match_fleet_trigger_contract(self):
        fields = experience.dispatch_fields(experience.brief(sig(), [case()], []))
        self.assertEqual(set(fields), {'mission', 'priority_districts', 'downwind_front', 'tactical_constraints'})
        self.assertEqual(json.loads(fields['priority_districts']), ['farm'])
        self.assertIn('north', fields['downwind_front'])
        self.assertTrue(all(isinstance(v, str) for v in fields.values()))

    def test_calm_and_unconfirmed_front(self):
        front = experience.describe_front(sig(wind=[0, 0], wind_strength=0., fire_confirmed=False, fire_centroid=None, believed_fire_cells=0))
        self.assertIn('calm', front)
        self.assertIn('not yet observed', front)

    def test_no_exposure_mission_still_names_unwarned(self):
        b = experience.brief(sig(districts=dict(town=dict(status='unwarned', distance=40., downwind=0., people=4000))), [], [])
        self.assertEqual(b['priority_districts'], [])
        self.assertIn('town', experience.dispatch_fields(b)['mission'])
        self.assertIn('not exposed yet: town', b['text'])


if __name__ == '__main__':
    unittest.main()
