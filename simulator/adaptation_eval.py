"""Adaptation benchmark across scenario families: ignition, wind, fleet and changed worlds.

Measures the pieces the agent is *given* at decision time, not the language model:

* ``brief``: follow the deterministic episode brief (priority districts ranked by people x
  forecast threat / downwind / proximity). Tests the context engineering.
* ``warning``: downwind-by-population heuristic. ``hold``: do nothing.
  ``oracle_plan_replay``: copy the hindsight-preferred plan of a close past case.
* Retrieval coverage: how often memory offers a case within MAX_CASE_DISTANCE, and
  whether it shares wind/people labels with the test situation.
* Divergence calibration: does the surprise check fire when the wind really changed
  (recall) and stay quiet when nothing changed (false alarms)?

Memory is trained on seeds 9/17 only (hold and warning decisions); test seeds are
disjoint and never enter memory. Regret is relative to the bounded hindsight search
plus the tested plans. This is a simulator benchmark, not evidence about HappyRobot.

    python -m simulator.adaptation_eval --output .runtime/adaptation-eval
"""
import argparse
import copy
import json
from pathlib import Path
import statistics
import time

from . import episodes, evaluation, experience, oracle, worlds
from .blackbox import BlackBox
from .engine import Simulation

TRAIN_SEEDS = evaluation.TRAIN_SEEDS
TEST_SEEDS = evaluation.TEST_SEEDS
ROLLOUT_SEEDS = evaluation.ROLLOUT_SEEDS
BASE = dict(ignition='default', wind='north', fleet='light')
FLEETS = dict(light=dict(scouts=1, extinguishers=1, trucks=0),
              truck=dict(scouts=1, extinguishers=1, trucks=1),
              drones=dict(scouts=0, extinguishers=2, trucks=0),
              full=dict(scouts=1, extinguishers=2, trucks=1))
WINDS = dict(north=(0, -1), east=(1, 0), south=(0, 1), west=(-1, 0), calm=(0, 0), strong_north=(0, -3), northeast=(1, -1))
IGNITIONS = ('default', 'near_town_north', 'near_farm')
SHIFTS = dict(north='east', east='south', south='west', west='north', calm='north', strong_north='east', northeast='south')
TRAIN_WINDS = ('north', 'east')
TRAIN_FLEETS = ('light', 'truck')


def families():
    """One-axis-at-a-time designs around BASE plus a wind-shift family; the base itself appears once."""
    configs = [dict(BASE, family='base', shift=False)]
    configs += [dict(BASE, ignition=i, family='ignition', shift=False) for i in IGNITIONS if i != BASE['ignition']]
    configs += [dict(BASE, wind=w, family='wind', shift=False) for w in WINDS if w != BASE['wind']]
    configs += [dict(BASE, fleet=f, family='fleet', shift=False) for f in FLEETS if f != BASE['fleet']]
    configs += [dict(BASE, wind=w, family='wind_shift', shift=True) for w in ('north', 'east')]
    return configs


def build(seed, ignition='default', wind='north', fleet='light', steps=2):
    sim = Simulation(seed=seed, fleet_counts=dict(FLEETS[fleet]))
    if ignition != 'default':
        target = sim.groups['town_north' if ignition == 'near_town_north' else 'farm']['home']
        offset = (15, 0) if ignition == 'near_town_north' else (0, 12)
        candidates = [(x, y) for y in range(1, sim.height-1) for x in range(1, sim.width-1)
                      if sim.cells[y][x]['fuel'] > 0 and (x-sim.base[0])**2+(y-sim.base[1])**2 >= 36]
        origin = min(candidates, key=lambda p: (p[0]-(target[0]+offset[0]))**2+(p[1]-(target[1]+offset[1]))**2)
        sim.place_fire(*origin)
    sim.set_wind(x=WINDS[wind][0], y=WINDS[wind][1])
    sim.ignite()
    sim.farmer_call()
    sim.step(steps)
    return sim


def _warn_order(options, district):
    return next((o for o in options if o.get('district_id') == district), None)


def brief_agent(sim, state, box):
    """Warn the brief's priority districts in order (people x exposure), contain with what is left."""
    view = state.get('forecast_view')
    sig = experience.signature(sim, state.get('event_type', 'local_observation'))
    priorities = [d['district_id'] for d in experience.rank_districts(sig, view)]
    parts = []
    for key, options in oracle.vehicle_options(sim):
        chosen = None
        for district in priorities:
            chosen = _warn_order(options, district)
            if chosen:
                priorities.remove(district)
                break
        if chosen is None and key == 'extinguisher_orders' and sig['fire_confirmed']:
            chosen = next((o for o in options if o['command'] == 'contain'), None)
        if chosen is None and key == 'truck_orders':
            chosen = next((o for o in options if o['command'] == 'attack_sector'), None)
        parts.append((key, chosen or options[0]))
    plan = oracle.assemble(parts)
    return plan if oracle.is_valid(sim, plan) else episodes.hold_orders(sim)


POLICIES = dict(hold=episodes.hold_agent, warning=evaluation.warning_agent,
                brief=brief_agent, oracle_plan_replay=episodes.experience_agent)


def train(box, horizon, log=print):
    rows = []
    for seed in TRAIN_SEEDS:
        for wind in TRAIN_WINDS:
            for fleet in TRAIN_FLEETS:
                sim = build(seed, wind=wind, fleet=fleet)
                for name in ('hold', 'warning'):
                    world = copy.deepcopy(sim)
                    plan = POLICIES[name](worlds.belief_world(world), dict(event_type='farmer_call'), box)
                    ev = oracle.evaluate(world, plan, horizon=horizon, seeds=ROLLOUT_SEEDS, time_budget=120.)
                    if ev['truncated']:
                        raise RuntimeError('Training search truncated.')
                    did = box.record_decision(world, world.payload('farmer_call'), None, 0., plan, 'applied')
                    box.save_case(did, world.incident_id, experience.signature(world, 'farmer_call'))
                    box.save_evaluation(did, ev)
                    world.apply(plan, 'training', world.incident_id, world.tick)
                    world.step(horizon)
                    box.finish_outcome(did, world)
                    rows.append(dict(seed=seed, wind=wind, fleet=fleet, policy=name, decision_id=did, regret=ev['regret']))
                    log(f'Train seed={seed} wind={wind} fleet={fleet} {name}: regret={ev["regret"]}', flush=True)
    return rows


def calibration(sim, forecast_result, shift_to):
    """Run truth two checkpoints ahead, unchanged and with a wind shift; report whether the surprise check fires."""
    out = {}
    for label, shift in (('stable', None), ('shifted', shift_to)):
        world = copy.deepcopy(sim)
        if shift:
            world.set_wind(x=WINDS[shift][0], y=WINDS[shift][1])
        world.step(2*worlds.CHECKPOINT_EVERY)
        check = worlds.surprise(forecast_result, world)
        out[label] = dict(distance=check['distance'], threshold=check['threshold'], divergent=check['divergent'],
                          what_changed=check['what_changed'][:3]) if check else None
    return out


def evaluate_snapshot(box, sim, config, seed, horizon, check_calibration):
    event = 'forecast_update' if config['shift'] and sim.tick > 2 else 'local_observation'
    sig = experience.signature(sim, event)
    cases = experience.retrieve(box, sig, exclude_incident=sim.incident_id)
    forecast_result = worlds.forecast(sim)
    view = worlds.agent_view(forecast_result)
    state = dict(similar_cases=cases, forecast_view=view, event_type=event, policy_seed=seed*1000+sim.tick)
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
        samples = [evaluation.outcome(sim, plan, horizon, s) for s in ROLLOUT_SEEDS] if valid else []
        measured[name] = dict(valid=valid, samples=samples, mean_cost=statistics.mean(s['cost'] for s in samples) if samples else None)
    best = min([reference['best_cost']]+[r['mean_cost'] for r in measured.values() if r['valid']])
    nearest = cases[0] if cases else None
    retrieval = dict(cases=len(cases), nearest=nearest['similarity_distance'] if nearest else None,
                     shares_wind=bool(nearest) and any(l.startswith('wind:') and l in nearest['matching_labels'] and l not in ('wind:light', 'wind:strong') for l in sig['labels']),
                     shares_people=bool(nearest) and any(l.startswith('people:') and l in nearest['matching_labels'] for l in sig['labels']),
                     nearest_regret=nearest['regret'] if nearest else None)
    priority = experience.rank_districts(sig, view)
    rows = []
    for name, result in measured.items():
        means = {key: round(statistics.mean(s[key] for s in result['samples']), 3)
                 for key in ('burned_cells', 'burnt_people', 'safe_people', 'unwarned_people')} if result['valid'] else {}
        rows.append(dict(family=config['family'], ignition=config['ignition'], wind=config['wind'], fleet=config['fleet'], shift=config['shift'],
                         seed=seed, tick=sim.tick, wind_now=list(sim.wind), policy=name, valid=result['valid'],
                         regret=round(max(0., result['mean_cost']-best), 3) if result['valid'] else None,
                         mean_cost=result['mean_cost'], reference_cost=best, outcomes=means, plan=plans[name],
                         policy_seconds=timings[name], oracle_seconds=reference['elapsed_s'],
                         retrieval=retrieval, priority=[d['district_id'] for d in priority],
                         forecast=dict(dispersion=view['dispersion'], expected_burning_cells=view['expected_burning_cells'],
                                       threatened=[k for k, v in (view.get('districts') or {}).items() if v['p_fire_within_8'] >= .5]),
                         calibration=calibration(sim, forecast_result, SHIFTS[config['wind']]) if check_calibration else None))
    return rows


def summarize(rows):
    policies = {}
    for name in POLICIES:
        selected = [r for r in rows if r['policy'] == name]
        regrets = [r['regret'] for r in selected if r['regret'] is not None]
        policies[name] = dict(decisions=len(selected), invalid=sum(not r['valid'] for r in selected),
                              mean_regret=round(statistics.mean(regrets), 3) if regrets else None,
                              zero_regret=sum(r == 0 for r in regrets), max_regret=max(regrets) if regrets else None,
                              by_family={f: round(statistics.mean(r['regret'] for r in selected if r['family'] == f and r['valid']), 3)
                                         for f in sorted({r['family'] for r in selected if r['valid']})})

    def paired(a, b):
        diffs = []
        for row in rows:
            if row['policy'] != a or not row['valid']:
                continue
            other = next(r for r in rows if r['policy'] == b and (r['family'], r['ignition'], r['wind'], r['fleet'], r['seed'], r['tick'])
                         == (row['family'], row['ignition'], row['wind'], row['fleet'], row['seed'], row['tick']))
            if other['valid']:
                diffs.append(round(other['regret']-row['regret'], 3))
        return dict(better=sum(d > 0 for d in diffs), tied=sum(d == 0 for d in diffs), worse=sum(d < 0 for d in diffs))

    snapshots = [r for r in rows if r['policy'] == 'hold']
    by_family = {}
    for f in sorted({r['family'] for r in snapshots}):
        sel = [r for r in snapshots if r['family'] == f]
        by_family[f] = dict(snapshots=len(sel), with_case=sum(r['retrieval']['cases'] > 0 for r in sel),
                            shares_wind=sum(bool(r['retrieval']['shares_wind']) for r in sel),
                            shares_people=sum(bool(r['retrieval']['shares_people']) for r in sel),
                            mean_nearest=round(statistics.mean(r['retrieval']['nearest'] for r in sel if r['retrieval']['nearest'] is not None), 4)
                            if any(r['retrieval']['nearest'] is not None for r in sel) else None)
    checks = [r['calibration'] for r in snapshots if r['calibration']]
    stable = [c['stable'] for c in checks if c['stable']]
    shifted = [c['shifted'] for c in checks if c['shifted']]
    return dict(policies=policies,
                brief_vs_hold=paired('brief', 'hold'), brief_vs_warning=paired('brief', 'warning'),
                replay_vs_hold=paired('oracle_plan_replay', 'hold'), warning_vs_hold=paired('warning', 'hold'),
                retrieval=by_family,
                divergence=dict(checks=len(checks), false_alarms=sum(c['divergent'] for c in stable), stable=len(stable),
                                detected=sum(c['divergent'] for c in shifted), shifted=len(shifted),
                                stable_mean_distance=round(statistics.mean(c['distance'] for c in stable), 4) if stable else None,
                                shifted_mean_distance=round(statistics.mean(c['distance'] for c in shifted), 4) if shifted else None))


def run(output, horizon=16, test_seeds=TEST_SEEDS, configs=None, ticks=(2, 10), log=print):
    if set(TRAIN_SEEDS) & set(test_seeds):
        raise ValueError('Training and test seeds must be disjoint.')
    directory = Path(output)
    directory.mkdir(parents=True, exist_ok=False)
    box = BlackBox(directory/'memory.sqlite')
    try:
        training = train(box, horizon, log)
        before = box.db.execute('SELECT COUNT(*) FROM decisions').fetchone()[0]
        rows = []
        for config in configs or families():
            for seed in test_seeds:
                sim = build(seed, config['ignition'], config['wind'], config['fleet'])
                for tick in ticks:
                    if tick > 2:
                        sim.step(tick-2)
                        if config['shift']:
                            shifted = SHIFTS[config['wind']]
                            sim.set_wind(x=WINDS[shifted][0], y=WINDS[shifted][1])
                    rows.extend(evaluate_snapshot(box, sim, config, seed, horizon, check_calibration=(tick == 2)))
                    log(f"Test {config['family']} ign={config['ignition']} wind={config['wind']} fleet={config['fleet']} seed={seed} t={sim.tick}: "
                        + str({r['policy']: r['regret'] for r in rows[-len(POLICIES):]}), flush=True)
        after = box.db.execute('SELECT COUNT(*) FROM decisions').fetchone()[0]
        if before != after:
            raise RuntimeError('Test data contaminated training memory.')
        result = dict(method='matched frozen snapshots per scenario family; simulator policies, not an LLM test',
                      train_seeds=list(TRAIN_SEEDS), test_seeds=list(test_seeds), rollout_seeds=list(ROLLOUT_SEEDS), horizon=horizon,
                      training_cases=before, training=training, rows=rows, summary=summarize(rows))
        (directory/'results.json').write_text(json.dumps(result, indent=2)+'\n')
        return result
    finally:
        box.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--output', default='.runtime/adaptation-eval')
    parser.add_argument('--horizon', type=int, default=16)
    parser.add_argument('--families', nargs='+', choices=('base', 'ignition', 'wind', 'fleet', 'wind_shift'))
    args = parser.parse_args()
    if Path(args.output).exists():
        parser.error('--output must be a new directory; existing results are preserved')
    configs = [c for c in families() if not args.families or c['family'] in args.families]
    result = run(args.output, args.horizon, configs=configs)
    print(json.dumps(result['summary'], indent=2))


if __name__ == '__main__':
    main()
