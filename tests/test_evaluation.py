import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from simulator import episodes, evaluation, oracle, worlds


class EvaluationTests(unittest.TestCase):
    def test_outcomes_match_oracle_cost_and_do_not_mutate_snapshot(self):
        sim = evaluation.scenario(23, 'north')
        before = copy.deepcopy(sim.state())
        plan = episodes.hold_orders(sim)
        for seed in (101, 211):
            measured = evaluation.outcome(sim, plan, 2, seed)
            self.assertEqual(measured['cost'], oracle.rollout(sim, plan, 2, seed))
            self.assertGreaterEqual(measured['burned_cells'], 0)
            self.assertGreaterEqual(measured['burnt_people'], 0)
        self.assertEqual(sim.state(), before)

    def test_belief_policies_are_valid_reproducible_and_do_not_use_test_grades(self):
        sim = worlds.belief_world(evaluation.scenario(23, 'north'))
        state = dict(policy_seed=23002, similar_cases=[])
        for agent in (evaluation.random_agent, evaluation.warning_agent, episodes.experience_agent):
            first = agent(copy.deepcopy(sim), state, None)
            self.assertEqual(first, agent(copy.deepcopy(sim), state, None))
            self.assertTrue(oracle.is_valid(sim, first))
        sim.set_wind('south')
        warning = evaluation.warning_agent(sim, state, None)
        self.assertTrue(oracle.is_valid(sim, warning))

    def test_duplicate_reservations_are_reported_even_when_the_plan_is_accepted(self):
        sim = evaluation.scenario(23, 'north')
        parts = [(key, next(order for order in orders if order.get('district_id') == 'farm'))
                 for key, orders in oracle.vehicle_options(sim)]
        plan = oracle.assemble(parts)
        self.assertTrue(oracle.is_valid(sim, plan))
        self.assertEqual(evaluation.outcome(sim, plan, 2, 101)['coordination_adjustments'], 1)

    def test_no_overlap_and_no_overwriting_existing_results(self):
        with TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, 'disjoint'):
                evaluation.run(Path(tmp)/'invalid', test_seeds=(9,))
            with self.assertRaises(FileExistsError):
                evaluation.run(tmp)

    def test_frozen_memory_and_paired_scenario_summary(self):
        def train(box, horizon, log):
            sim = evaluation.scenario(9, 'north')
            plan = episodes.hold_orders(sim)
            box.record_decision(sim, sim.payload(), None, 0, plan, 'applied')
            return []

        def grade(box, sim, scenario, seed, horizon):
            return [dict(policy=name, valid=True, regret=10 if name == 'hold' else 0,
                         scenario=scenario, seed=seed, tick=sim.tick, wind=list(sim.wind))
                    for name in evaluation.POLICIES]

        with TemporaryDirectory() as tmp, patch.object(evaluation, 'train', side_effect=train), \
             patch.object(evaluation, 'evaluate_snapshot', side_effect=grade):
            result = evaluation.run(Path(tmp)/'evaluation', test_seeds=(23,),
                                    scenarios=('north_to_east',), log=lambda *a, **kw: None)
            self.assertEqual(result['training_cases'], 1)
            self.assertEqual(result['memory_cases_after'], 1)
            self.assertEqual(result['summary']['replay_vs_hold']['better'], 2)
            self.assertEqual(result['rows'][0]['wind'], [0, -1])
            self.assertEqual(result['rows'][4]['wind'], [1, 0])
            self.assertEqual(json.loads((Path(tmp)/'evaluation/results.json').read_text()), result)

    def test_truncated_oracle_is_not_reported_as_zero_regret(self):
        sim = evaluation.scenario(23, 'north')
        with patch.object(evaluation.experience, 'retrieve', return_value=[]), \
             patch.object(evaluation.oracle, 'evaluate', return_value=dict(truncated=True)):
            with self.assertRaisesRegex(RuntimeError, 'truncated'):
                evaluation.evaluate_snapshot(None, sim, 'north', 23, 2)
