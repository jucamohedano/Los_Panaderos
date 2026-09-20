import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from simulator import experience
from simulator.blackbox import BlackBox
from tests.test_experience import graded, scenario
from tests.test_worlds import orders, unseen_fuel


class ExperienceLabelTests(unittest.TestCase):
    def test_labels_describe_belief_and_do_not_change_ranking(self):
        sim = scenario()
        sig = experience.signature(sim, 'farmer_call')
        self.assertIn('wind:north', sig['labels'])
        self.assertIn('phase:early', sig['labels'])
        self.assertIn('fleet:scouts:1', sig['labels'])
        for x, y in unseen_fuel(sim):
            sim.cells[y][x].update(heat=.6, age=0)
        self.assertEqual(experience.signature(sim, 'farmer_call')['labels'], sig['labels'])
        legacy = {k: v for k, v in sig.items() if k not in ('labels', 'label_version')}
        self.assertEqual(experience.signature_distance(sig, legacy)['total'], 0)
        self.assertEqual(experience.situation_labels(legacy), sig['labels'])

    def test_calm_strong_wind_and_exposed_people_labels(self):
        sig = experience.signature(scenario())
        sig.update(wind=[0, 0], wind_strength=0)
        self.assertIn('wind:calm', experience.situation_labels(sig))
        sig.update(wind=[3, -3], wind_strength=4.24, tick=35)
        sig['districts']['farm'].update(status='unwarned', distance=3, downwind=.9)
        labels = experience.situation_labels(sig)
        for label in ('wind:northeast', 'wind:strong', 'people:nearby', 'people:downwind', 'phase:later'):
            self.assertIn(label, labels)

    def test_old_sqlite_signatures_gain_match_and_difference_explanations(self):
        with TemporaryDirectory() as tmp:
            box = BlackBox(Path(tmp)/'memory.sqlite')
            self.addCleanup(box.close)
            past = scenario()
            did = graded(box, past, orders(past), 100)
            legacy = box.case_signature(did)
            legacy.pop('labels')
            legacy.pop('label_version')
            box.save_case(did, past.incident_id, legacy)
            now = copy.deepcopy(legacy)
            now['fleet']['scouts'] = 0
            cases = experience.retrieve(box, now)
            self.assertEqual(len(cases), 1)
            self.assertIn('wind:north', cases[0]['matching_labels'])
            self.assertIn('fleet:scouts:0', cases[0]['differing_labels']['current_only'])
            self.assertIn('fleet:scouts:1', cases[0]['differing_labels']['case_only'])
            self.assertEqual(cases[0]['label_version'], experience.LABEL_VERSION)
            self.assertNotIn('snapshot', json.dumps(cases))

    def test_incomplete_grades_are_not_returned_as_precedents(self):
        with TemporaryDirectory() as tmp:
            box = BlackBox(Path(tmp)/'memory.sqlite')
            self.addCleanup(box.close)
            sim = scenario()
            good = graded(box, sim, orders(sim), 0)
            for result in ({'regret': 0, 'truncated': True}, {}, {'regret': None},
                           {'regret': float('nan')}, {'regret': True}):
                did = graded(box, sim, orders(sim), 0)
                box.save_evaluation(did, result)
            self.assertEqual([c['decision_id'] for c in experience.retrieve(box, experience.signature(sim))], [good])
