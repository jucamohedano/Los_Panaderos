"""Episode harness: replay the same scenario several times and measure whether experience lowers regret.

This is the learning-curve proof for the demo and for CI. It runs the incident loop
headless (no HTTP, no HappyRobot): for each decision it builds the payload exactly as
the controller does (possible worlds are skipped for speed, similar cases and relevant
lessons are included), asks an agent for a decision, freezes it in the black box, and
grades it with the hindsight oracle. Every episode starts from the same seed, so the
only thing that changes between episodes is the experience available to the agent.

Two agents are provided. `hold_agent` never learns and sets the baseline.
`experience_agent` mimics what we ask HappyRobot to do with `similar_cases`: when a
close past case says the oracle preferred another plan and that plan is still valid,
it adopts it; otherwise it holds. The gap between the two curves is the value of the
replay buffer, independent of any language model.

    python -m simulator.episodes --episodes 3
"""
import argparse
import copy
import json
from pathlib import Path
import time

from . import experience, oracle
from .blackbox import BlackBox
from .engine import Simulation

DECISION_EVERY = 8
ADOPT_DISTANCE = .15


def hold_orders(sim):
    return dict(primary_command='hold', mission='baseline', drone_reason='hold',
                extinguisher_orders=[dict(drone_id=d['drone_id'], command='hold', target_x=int(d['x']), target_y=int(d['y']), district_id='', reason='hold') for d in sim.extinguishers],
                scout_orders=[dict(drone_id=s['drone_id'], command='hold', waypoints=[], reason='hold') for s in sim.scouts],
                truck_orders=[dict(truck_id=t['truck_id'], command='continue', reason='hold') for t in sim.trucks])


def hold_agent(sim, state, box):
    return hold_orders(sim)


def experience_agent(sim, state, box):
    """Adopt the oracle-preferred plan of the closest similar case when it applies here."""
    for case in state.get('similar_cases') or []:
        if case['similarity_distance'] > ADOPT_DISTANCE or not case.get('regret') or not case.get('oracle_preferred'):
            continue
        evaluation = box.evaluation(case['decision_id']) or {}
        best = evaluation.get('best_decision')
        if best and oracle.is_valid(sim, best):
            plan = copy.deepcopy(best)
            plan['mission'] = f"from case {case['decision_id']} (regret {case['regret']})"
            return plan
    return hold_orders(sim)


def run_episode(box, agent, seed=9, decisions=3, horizon=8, oracle_budget=10., fleet_counts=None, log=print):
    sim = Simulation(seed=seed, fleet_counts=fleet_counts or dict(scouts=1, extinguishers=1, trucks=1))
    sim.set_wind('north')
    sim.ignite()
    sim.farmer_call()
    sim.step(2)
    graded = []
    for i in range(decisions):
        event = 'farmer_call' if i == 0 else 'local_observation'
        sig = experience.signature(sim, event)
        cases = experience.retrieve(box, sig, exclude_incident=sim.incident_id)
        lessons = experience.relevant_lessons(box, sig)
        sim.lessons = [l['rule'] for l in lessons]
        payload = sim.payload(event)
        state = json.loads(payload['world_state'])
        state['similar_cases'] = cases
        payload['world_state'] = json.dumps(state)
        decision = agent(sim, state, box)
        snapshot = copy.deepcopy(sim)
        decision_id = box.record_decision(sim, payload, None, 0., decision, 'pending')
        box.save_case(decision_id, sim.incident_id, sig)
        box.record_lesson_uses(decision_id, [l['id'] for l in lessons])
        try:
            sim.apply(decision, payload['event_id'], payload['incident_id'], sim.tick)
            box.set_status(decision_id, 'applied')
        except ValueError as exc:
            box.set_status(decision_id, 'rejected', str(exc)[:300])
        evaluation = oracle.evaluate(snapshot, decision, horizon=horizon, time_budget=oracle_budget)
        box.save_evaluation(decision_id, evaluation)
        graded.append(evaluation.get('regret'))
        log(f"  t={sim.tick} {event}: {len(cases)} cases, regret {evaluation.get('regret')} ({evaluation.get('gap_type')}); {decision.get('mission')}")
        sim.step(DECISION_EVERY)
        box.finish_outcome(decision_id, sim)
    experience.review_lessons(box)
    return dict(incident_id=sim.incident_id, regrets=graded, people={k: g['status'] for k, g in sim.groups.items()})


def run(db, episodes=3, agent=experience_agent, log=print, **kw):
    box = BlackBox(db)
    try:
        out = []
        for n in range(episodes):
            log(f'Episode {n+1}/{episodes}')
            started = time.monotonic()
            out.append(run_episode(box, agent, log=log, **kw))
            log(f"  regrets {out[-1]['regrets']} in {time.monotonic()-started:.1f}s")
        return dict(runs=out, curve=experience.learning_curve(box))
    finally:
        box.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--episodes', type=int, default=3)
    ap.add_argument('--db', default='.runtime/episodes.sqlite')
    ap.add_argument('--agent', choices=('experience', 'hold'), default='experience')
    ap.add_argument('--fresh', action='store_true', help='delete the database first')
    args = ap.parse_args()
    if args.fresh and Path(args.db).exists():
        Path(args.db).unlink()
    result = run(args.db, args.episodes, experience_agent if args.agent == 'experience' else hold_agent)
    print(json.dumps([dict(incident=e['incident_id'][:8], mean_regret=e['mean_regret'], cases=e['cases_available'], lessons=e['lessons_shown']) for e in result['curve']], indent=1))


if __name__ == '__main__':
    main()
