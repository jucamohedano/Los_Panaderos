"""Held-out, matched-snapshot benchmark of SQLite case replay, not LLM learning.

Train only on seeds 9/17 with north/east winds; freeze memory before evaluating
seeds 23/47/81, including south, calm and a north-to-east shift. Every policy sees
the same snapshot and is scored on the same future RNG seeds. No test outcomes
enter retrieval. Regret is relative to a bounded hindsight search plus the tested
plans, not a globally optimal policy.
"""
import argparse
import copy
import itertools
import json
from pathlib import Path
import random
import statistics
import time

from . import episodes, experience, oracle, worlds
from .blackbox import BlackBox
from .engine import Simulation

TRAIN_SEEDS = (9, 17)
TEST_SEEDS = (23, 47, 81)
ROLLOUT_SEEDS = (101, 211, 307)
SCENARIOS = ('north', 'east', 'south', 'calm', 'north_to_east', 'town_west', 'strong_north', 'single_drone')


def scenario(seed, wind):
    sim = Simulation(seed=seed, fleet_counts=dict(scouts=0 if wind == 'single_drone' else 1, extinguishers=1, trucks=0))
    if wind == 'town_west':
        gx, gy = sim.groups['town_north']['home']
        candidates = [(x, y) for y in range(1, sim.height-1) for x in range(1, sim.width-1)
                      if sim.cells[y][x]['fuel'] > 0 and (x-sim.base[0])**2+(y-sim.base[1])**2 >= 36]
        origin = min(candidates, key=lambda p: (p[0]-(gx+15))**2+(p[1]-gy)**2)
        sim.place_fire(*origin)
        sim.set_wind(x=-3, y=0)
    elif wind == 'strong_north':
        sim.set_wind(x=0, y=-3)
    else:
        sim.set_wind('north' if wind in ('north_to_east', 'single_drone') else wind)
    sim.ignite()
    sim.farmer_call()
    sim.step(2)
    return sim


def random_agent(sim, state, box):
    rng = random.Random(state['policy_seed'])
    options = oracle.vehicle_options(sim)
    combinations = list(itertools.product(*[[(key, order) for order in opts] for key, opts in options]))
    rng.shuffle(combinations)
    for parts in combinations:
        plan = oracle.assemble(parts)
        if oracle.is_valid(sim, plan):
            return plan
    return episodes.hold_orders(sim)


def warning_agent(sim, state, box):
    """Warn downwind unwarned districts by population, then believed distance."""
    sig = experience.signature(sim)
    priorities = sorted(
        (key for key, district in sig['districts'].items()
         if district['status'] == 'unwarned' and district['downwind'] >= oracle.SECTOR_COS),
        key=lambda key: (-sig['districts'][key]['people'], sig['districts'][key]['distance']),
    )
    parts = []
    for key, options in oracle.vehicle_options(sim):
        chosen = options[0]
        for district in priorities:
            match = next((order for order in options if order.get('district_id') == district), None)
            if match:
                chosen = match
                priorities.remove(district)
                break
        parts.append((key, chosen))
    plan = oracle.assemble(parts)
    return plan if oracle.is_valid(sim, plan) else episodes.hold_orders(sim)


POLICIES = dict(hold=episodes.hold_agent, random=random_agent,
                warning=warning_agent, oracle_plan_replay=episodes.experience_agent)


def outcome(snapshot, plan, horizon, seed):
    sim = copy.deepcopy(snapshot)
    sim.rng = random.Random(seed)
    sim.suppression_rng = random.Random(seed+1)
    before = oracle._metrics(sim)
    safe_before = sum(g['count'] for g in sim.groups.values() if g['status'] == 'safe')
    applied = sim.apply(copy.deepcopy(plan), f'evaluation-{seed}', sim.incident_id, sim.tick)
    sim.step(horizon)
    after = oracle._metrics(sim)
    return dict(cost=oracle.cost(sim, before), coordination_adjustments=len(applied.get('coordination_adjustments', [])),
                burned_cells=after['burned']-before['burned'],
                burnt_people=after['burnt']-before['burnt'],
                safe_people=sum(g['count'] for g in sim.groups.values() if g['status'] == 'safe')-safe_before,
                unwarned_people=sum(g['count'] for g in sim.groups.values() if g['status'] == 'unwarned'))


def train(box, horizon, log=print):
    evaluations = []
    for seed in TRAIN_SEEDS:
        for wind in ('north', 'east'):
            sim = scenario(seed, wind)
            plan = episodes.hold_orders(sim)
            evaluation = oracle.evaluate(sim, plan, horizon=horizon, seeds=ROLLOUT_SEEDS, time_budget=120.)
            if evaluation['truncated']:
                raise RuntimeError('Training search truncated; do not present this as a complete benchmark.')
            did = box.record_decision(sim, sim.payload('farmer_call'), None, 0., plan, 'applied')
            box.save_case(did, sim.incident_id, experience.signature(sim, 'farmer_call'))
            box.save_evaluation(did, evaluation)
            sim.apply(plan, 'training', sim.incident_id, sim.tick)
            sim.step(horizon)
            box.finish_outcome(did, sim)
            evaluations.append(dict(seed=seed, wind=wind, decision_id=did, evaluation=evaluation))
            log(f'Train seed={seed} wind={wind}: regret={evaluation["regret"]}', flush=True)
    return evaluations


def evaluate_snapshot(box, sim, scenario_name, seed, horizon):
    event = 'forecast_update' if scenario_name == 'north_to_east' and sim.tick > 2 else 'local_observation'
    sig = experience.signature(sim, event)
    cases = experience.retrieve(box, sig, exclude_incident=sim.incident_id)
    state = dict(similar_cases=cases, policy_seed=seed*1000+sim.tick)
    belief = worlds.belief_world(sim)
    plans, timings = {}, {}
    for name, agent in POLICIES.items():
        start = time.monotonic()
        plans[name] = agent(copy.deepcopy(belief), state, box)
        timings[name] = round(time.monotonic()-start, 4)
    reference = oracle.evaluate(sim, plans['hold'], horizon=horizon, seeds=ROLLOUT_SEEDS, time_budget=120.)
    if reference['truncated']:
        raise RuntimeError('Test search truncated; increase budget before drawing conclusions.')
    measured = {}
    for name, plan in plans.items():
        valid = oracle.is_valid(sim, plan)
        samples = [outcome(sim, plan, horizon, s) for s in ROLLOUT_SEEDS] if valid else []
        measured[name] = dict(valid=valid, samples=samples, mean_cost=statistics.mean(s['cost'] for s in samples) if samples else None)
    best = min([reference['best_cost']]+[r['mean_cost'] for r in measured.values() if r['valid']])
    rows = []
    for name, result in measured.items():
        means = {key: round(statistics.mean(s[key] for s in result['samples']), 3)
                 for key in ('burned_cells', 'burnt_people', 'safe_people', 'unwarned_people', 'coordination_adjustments')} if result['valid'] else {}
        rows.append(dict(scenario=scenario_name, seed=seed, tick=sim.tick, wind=list(sim.wind),
                         policy=name, valid=result['valid'],
                         regret=round(max(0., result['mean_cost']-best), 3) if result['valid'] else None,
                         mean_cost=result['mean_cost'], reference_cost=best, outcomes=means,
                         samples=result['samples'], plan=plans[name], policy_seconds=timings[name],
                         oracle_seconds=reference['elapsed_s'], candidate_count=reference['candidate_count'],
                         oracle_truncated=reference['truncated'],
                         origin=list(sim.report), fleet_counts=sim.fleet_counts(),
                         retrieved=[dict(id=c['decision_id'], distance=c['similarity_distance']) for c in cases]))
    return rows


def summarize(rows):
    summary = {}
    for name in POLICIES:
        selected = [row for row in rows if row['policy'] == name]
        regrets = [row['regret'] for row in selected if row['regret'] is not None]
        summary[name] = dict(decisions=len(selected), invalid=sum(not row['valid'] for row in selected),
                             mean_regret=round(statistics.mean(regrets), 3) if regrets else None,
                             max_regret=max(regrets) if regrets else None,
                             by_scenario={scenario: round(statistics.mean(
                                 row['regret'] for row in selected if row['scenario'] == scenario and row['valid']), 3)
                                 for scenario in sorted({r['scenario'] for r in selected if r['valid']})})
    paired = []
    for row in rows:
        if row['policy'] != 'oracle_plan_replay' or not row['valid']:
            continue
        baseline = next(r for r in rows if r['policy'] == 'hold' and
                        (r['scenario'], r['seed'], r['tick']) == (row['scenario'], row['seed'], row['tick']))
        paired.append(round(baseline['regret']-row['regret'], 3))
    return dict(policies=summary, replay_vs_hold=dict(better=sum(d>0 for d in paired),
                tied=sum(d==0 for d in paired), worse=sum(d<0 for d in paired), paired_regret_reduction=paired))


def run(output, horizon=16, test_seeds=TEST_SEEDS, scenarios=SCENARIOS, log=print):
    if set(TRAIN_SEEDS) & set(test_seeds):
        raise ValueError('Training and test seeds must be disjoint.')
    directory = Path(output)
    directory.mkdir(parents=True, exist_ok=False)
    box = BlackBox(directory/'memory.sqlite')
    try:
        training = train(box, horizon, log)
        before = box.db.execute('SELECT COUNT(*) FROM decisions').fetchone()[0]
        rows = []
        for seed in test_seeds:
            for wind in scenarios:
                sim = scenario(seed, wind)
                for tick in (2, 10):
                    if tick == 10:
                        sim.step(8)
                        if wind == 'north_to_east':
                            sim.set_wind('east')
                    rows.extend(evaluate_snapshot(box, sim, wind, seed, horizon))
                    log(f'Test seed={seed} wind={wind} t={tick}: '+str({r['policy']: r['regret'] for r in rows[-len(POLICIES):]}), flush=True)
        after = box.db.execute('SELECT COUNT(*) FROM decisions').fetchone()[0]
        if before != after:
            raise RuntimeError('Test data contaminated training memory.')
        result = dict(method='matched frozen snapshots; not sequential policy trajectories or an LLM test',
                      train_seeds=list(TRAIN_SEEDS), test_seeds=list(test_seeds), rollout_seeds=list(ROLLOUT_SEEDS),
                      horizon=horizon, training_cases=before, memory_cases_after=after,
                      training=training, rows=rows, summary=summarize(rows))
        (directory/'results.json').write_text(json.dumps(result, indent=2)+'\n')
        return result
    finally:
        box.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--output', default='.runtime/learning-evaluation')
    parser.add_argument('--horizon', type=int, default=16)
    parser.add_argument('--scenarios', nargs='+', choices=SCENARIOS, default=SCENARIOS)
    args = parser.parse_args()
    if args.horizon < 1:
        parser.error('--horizon must be positive')
    if Path(args.output).exists():
        parser.error('--output must be a new directory; existing results are preserved')
    result = run(args.output, args.horizon, scenarios=args.scenarios)
    print(json.dumps(result['summary'], indent=2))


if __name__ == '__main__':
    main()
