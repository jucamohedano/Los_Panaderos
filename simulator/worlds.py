"""Possible worlds: forward ensembles from the agent's belief and a distance between worlds.

The hindsight oracle grades a decision after the fact with ground truth. This module
looks forward instead: it builds the world the agent is entitled to know (sensor memory,
delayed satellite, the smoke report; nothing hidden), forks it into reseeded branches,
rolls each forward and summarises where the fire is likely to be, which districts are
threatened and how much the branches disagree. The same distance that measures that
disagreement later measures how far reality drifted from the forecast; a large drift
is a `forecast_divergence` decision event, so the agent replans against a named,
invalidated assumption rather than on a fixed timer.

Everything here is a demo heuristic on the educational engine, not an operational
forecast. Weights and thresholds are configuration, kept in one place.
"""
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
import copy
import math
import multiprocessing
import os
import random
import threading

WEIGHTS = dict(fire=.25, burned=.15, people=.40, threat=.15, fleet=.05)
PEOPLE_RANK = dict(safe=0, evacuating=1, unwarned=2, blocked=3, burnt=4)
THREAT_CELLS = 8
FRONT_TOLERANCE = 2
DEFAULT_HORIZON = 16
DEFAULT_BRANCHES = 8
CHECKPOINT_EVERY = 4
SEED_BASE = 1000
# Observation is "surprising" when it sits further from the forecast medoid than the
# branches sit from each other (times DIVERGENCE_RATIO), bounded by a floor (tiny
# forecasts are not punished for noise) and a ceiling (a very wide forecast still
# cannot explain a fire at a district's edge).
DIVERGENCE_RATIO = 2.0
DIVERGENCE_FLOOR = 0.05
DIVERGENCE_CEILING = 0.30

_pool = None
_pool_lock = threading.Lock()


def _workers():
    """Branches are CPU-bound (truck routing dominates), so they run in a lazily started process pool."""
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ProcessPoolExecutor(max_workers=max(1, min(DEFAULT_BRANCHES, os.cpu_count() or 1)),
                                        mp_context=multiprocessing.get_context('forkserver'))
        return _pool


def belief_world(sim):
    """Copy of the world holding only what the agent could know at this tick.

    Observed cells take the remembered intensity/burn/wetness (stale ones included);
    unobserved cells are assumed unburnt with their static fuel. A satellite block
    reporting fire with no remembered fire inside seeds one ignition at its centre;
    if the belief holds no fire at all, the smoke report is taken at face value.
    """
    world = copy.deepcopy(sim)
    world.history = []
    world.satellite_queue = []
    for y, row in enumerate(world.cells):
        for x, cell in enumerate(row):
            remembered = world.memory.get(f'{x},{y}')
            initial = cell.get('initial_fuel', cell['fuel'])
            if remembered is None:
                cell.update(heat=0., age=0, wet=0, burned=0., fuel=initial)
                continue
            burned = float(remembered.get('burned_fraction', 0.))
            cell.update(heat=float(remembered.get('intensity', .25)) if remembered['burning'] else 0., age=0,
                        wet=int(remembered.get('wet_steps_remaining', 0)), burned=burned, fuel=max(0., initial-burned*initial))
            if remembered['burning'] and cell['fuel'] <= 0:
                cell['fuel'] = .2
    if world.satellite:
        for bx, by in world.satellite.get('blocks', []):
            block = [(x, y) for y in range(by, min(world.height, by+8)) for x in range(bx, min(world.width, bx+8))]
            if any(world.burning(world.cells[y][x]) for x, y in block):
                continue
            cx, cy = bx+4, by+4
            for x, y in sorted(block, key=lambda p: math.dist(p, (cx, cy))):
                if world.cells[y][x]['fuel'] > 0:
                    world.cells[y][x].update(heat=.25, age=0)
                    break
    if world.called and not world.fire_points():
        rx, ry = world.report
        for x, y in ((rx, ry), (rx, ry+1), (rx+1, ry)):
            if 0 <= x < world.width and 0 <= y < world.height and world.cells[y][x]['fuel'] > 0:
                world.cells[y][x].update(heat=.25, age=0)
    world.observe()
    return world


def world_vector(sim):
    """Compact, comparable description of a world state."""
    fire = sorted(sim.fire_points())
    return dict(tick=sim.tick,
                fire=fire,
                burned=sorted((x, y) for y, row in enumerate(sim.cells) for x, c in enumerate(row) if c.get('burned', 0) > 0),
                people={k: g['status'] for k, g in sim.groups.items()},
                threat={k: round(min((math.dist(g['home'], p) for p in fire), default=math.hypot(sim.width, sim.height)), 2)
                        for k, g in sim.groups.items()},
                fleet={v.get('drone_id') or v.get('truck_id'): [round(v['x'], 1), round(v['y'], 1)] for v in sim.vehicles()},
                extinguished=sim.suppressed+sim.crew_extinguished)


def _dilate(points, radius):
    return {(x+dx, y+dy) for x, y in points for dx in range(-radius, radius+1) for dy in range(-radius, radius+1)}


def _front_distance(a, b, radius=FRONT_TOLERANCE):
    """1 minus the share of cells in either set that lie within ``radius`` of the other.

    Exact-cell Jaccard punishes a front that is one cell off; fronts are compared with
    a tolerance instead, so two branches of the same fire look alike.
    """
    a, b = {tuple(p) for p in a}, {tuple(p) for p in b}
    if not a and not b:
        return 0.
    near_b, near_a = _dilate(b, radius), _dilate(a, radius)
    matched = sum(p in near_b for p in a)+sum(p in near_a for p in b)
    return 1.-matched/(len(a)+len(b))


def unexpected_fire(actual, expected, radius=FRONT_TOLERANCE):
    """Cells burning now that lie beyond ``radius`` of every forecast fire cell."""
    near = _dilate({tuple(p) for p in expected}, radius)
    return [p for p in actual if tuple(p) not in near]


def distance(a, b, weights=WEIGHTS, diagonal=math.hypot(80, 56)):
    """Weighted distance in [0, 1] between two world vectors, with its terms."""
    districts = sorted(set(a['people']) | set(b['people'])) or ['none']
    people = sum(abs(PEOPLE_RANK.get(a['people'].get(k), 2)-PEOPLE_RANK.get(b['people'].get(k), 2)) for k in districts)/(4.*len(districts))
    threat = sum(abs(a['threat'].get(k, diagonal)-b['threat'].get(k, diagonal)) for k in districts)/(len(districts)*diagonal)
    ids = sorted(set(a['fleet']) | set(b['fleet']))
    fleet = (sum(math.dist(a['fleet'].get(i, (0, 0)), b['fleet'].get(i, (0, 0))) for i in ids)/(len(ids)*diagonal)) if ids else 0.
    terms = dict(fire=_front_distance(a['fire'], b['fire']), burned=_front_distance(a['burned'], b['burned']),
                 people=min(1., people), threat=min(1., threat), fleet=min(1., fleet))
    total = sum(weights[k]*terms[k] for k in weights)
    return dict(total=round(total, 4), terms={k: round(v, 4) for k, v in terms.items()})


def rollout(world, decision, horizon, seed, checkpoint_every=CHECKPOINT_EVERY):
    """Roll one branch forward; returns world vectors at each checkpoint (last = horizon)."""
    branch = copy.deepcopy(world)
    branch.rng = random.Random(seed)
    branch.suppression_rng = random.Random(seed+1)
    if decision:
        branch.apply(copy.deepcopy(decision), f'forecast-{seed}', branch.incident_id, branch.tick)
    checkpoints = {}
    for step in range(1, horizon+1):
        branch.step()
        if step % checkpoint_every == 0 or step == horizon:
            checkpoints[branch.tick] = world_vector(branch)
    return checkpoints


def _medoid(vectors):
    if len(vectors) == 1:
        return 0, 0.
    totals = [sum(distance(v, w)['total'] for w in vectors if w is not v) for v in vectors]
    index = min(range(len(vectors)), key=totals.__getitem__)
    pairs = len(vectors)*(len(vectors)-1)
    return index, round(sum(totals)/pairs, 4)


def summarise(branches, origin):
    """Ensemble statistics for one plan: burn probability, district threat, dispersion, medoid."""
    finals = [b[max(b)] for b in branches]
    n = len(finals)
    counts = {}
    for v in finals:
        for p in v['fire']:
            counts[tuple(p)] = counts.get(tuple(p), 0)+1
    known_now = {tuple(p) for p in origin['fire']}
    burn_probability = sorted(([x, y, round(c/n, 2)] for (x, y), c in counts.items() if (x, y) not in known_now), key=lambda t: -t[2])
    districts = {}
    for k in origin['people']:
        statuses = [v['people'][k] for v in finals]
        threats = [v['threat'][k] for v in finals]
        districts[k] = dict(p_fire_within_8=round(sum(t <= THREAT_CELLS for t in threats)/n, 2),
                            p_blocked_or_burnt=round(sum(s in ('blocked', 'burnt') for s in statuses)/n, 2),
                            expected_distance=round(sum(threats)/n, 1),
                            status_now=origin['people'][k],
                            outcomes={s: statuses.count(s) for s in sorted(set(statuses))})
    medoid_index, dispersion = _medoid(finals)
    medoid = branches[medoid_index]
    return dict(branches=n, dispersion=dispersion,
                expected_burning_cells=round(sum(len(v['fire']) for v in finals)/n, 1),
                expected_new_burned_cells=round(sum(len(v['burned']) for v in finals)/n-len(origin['burned']), 1),
                districts=districts, burn_probability=burn_probability[:400],
                medoid={str(t): v for t, v in medoid.items()})


def forecast(sim, plans=None, horizon=DEFAULT_HORIZON, branches=DEFAULT_BRANCHES, seed_base=SEED_BASE, parallel=True):
    """Ensemble forecast from the belief world for each named plan.

    ``plans`` maps a name to a decision (``None`` = keep current orders). Plans the
    validator rejects on the belief world are reported invalid, never scored.
    """
    plans = plans if plans is not None else {'current_orders': None}
    world = belief_world(sim)
    origin = world_vector(world)
    result = dict(issued_at=sim.tick, horizon=horizon, branches=branches, wind=list(sim.wind), spread_factor=sim.rules['spread_factor'],
                  believed_burning_cells=len(origin['fire']), true_burning_cells=len(sim.fire_points()), plans={})
    for name, decision in plans.items():
        seeds = [seed_base+k for k in range(branches)]
        try:
            runs = _rollouts(world, decision, horizon, seeds, parallel)
            result['plans'][name] = summarise(runs, origin) if runs else dict(valid=False, error='no branches')
        except (ValueError, KeyError, TypeError) as exc:
            result['plans'][name] = dict(valid=False, error=str(exc)[:200])
    return result


def _rollouts(world, decision, horizon, seeds, parallel):
    global _pool
    if parallel:
        try:
            return list(_workers().map(rollout, [world]*len(seeds), [decision]*len(seeds), [horizon]*len(seeds), seeds))
        except (BrokenProcessPool, OSError):
            with _pool_lock:
                _pool = None
    return [rollout(world, decision, horizon, seed) for seed in seeds]


def surprise(forecast_result, sim, plan='current_orders'):
    """Distance between what is now *observed* and the forecast medoid at the nearest checkpoint.

    The comparison uses the belief world, never ground truth: divergence means the
    agent's own evidence contradicts its forecast, not that hidden fire exists. Returns
    ``None`` when the forecast holds no comparable checkpoint yet. Divergence is flagged
    when the observed world is further from the medoid than the branches were from each
    other (times DIVERGENCE_RATIO) and above DIVERGENCE_FLOOR.
    """
    summary = (forecast_result or {}).get('plans', {}).get(plan)
    if not summary or not summary.get('medoid'):
        return None
    ticks = sorted(int(t) for t in summary['medoid'])
    if sim.tick < ticks[0]:
        return None
    nearest = min(ticks, key=lambda t: abs(t-sim.tick))
    medoid = summary['medoid'][str(nearest)]
    expected = dict(medoid)
    actual = world_vector(belief_world(sim))
    # Fire is compared only where the agent has looked since the forecast was issued:
    # both sides are cut to that footprint, so stale memory and unseen ground count for nothing.
    issued_at = forecast_result.get('issued_at', -1)
    footprint = {tuple(int(v) for v in k.split(',')): m for k, m in sim.memory.items() if m.get('observed_at', -1) >= issued_at}
    actual['fire'] = sorted(p for p, m in footprint.items() if m.get('burning'))
    actual['burned'] = sorted(p for p, m in footprint.items() if m.get('burned_fraction', 0) > 0)
    expected['fire'] = [p for p in expected['fire'] if tuple(p) in footprint]
    expected['burned'] = [p for p in expected['burned'] if tuple(p) in footprint]
    d = distance(expected, actual)
    dispersion = summary.get('dispersion', 0.)
    threshold = round(min(DIVERGENCE_CEILING, max(DIVERGENCE_FLOOR, DIVERGENCE_RATIO*dispersion)), 4)
    changed = []
    for k, status in actual['people'].items():
        if expected['people'].get(k) != status:
            changed.append(f"{k}: expected {expected['people'].get(k)}, now {status}")
    for k, t in actual['threat'].items():
        e = expected['threat'].get(k)
        if e is not None and (t <= THREAT_CELLS) != (e <= THREAT_CELLS):
            changed.append(f"{k}: fire {'within' if t <= THREAT_CELLS else 'beyond'} {THREAT_CELLS} cells (forecast {e:g}, now {t:g})")
    unexpected = unexpected_fire(actual['fire'], medoid['fire'])
    if unexpected:
        changed.append(f'{len(unexpected)} observed burning cells beyond the forecast front, e.g. {unexpected[0]}')
    if max(d['terms']['fire'], d['terms']['burned']) > .2:
        changed.append(f"observed front differs from forecast: {len(actual['fire'])} burning / {len(actual['burned'])} burned cells seen "
                       f"vs {len(expected['fire'])} / {len(expected['burned'])} forecast in the same footprint")
    if forecast_result.get('wind') is not None and list(sim.wind) != list(forecast_result['wind']):
        changed.append(f"wind changed from {forecast_result['wind']} to {list(sim.wind)}")
    return dict(tick=sim.tick, compared_to_tick=nearest, distance=d['total'], terms=d['terms'], dispersion=dispersion,
                threshold=threshold, divergent=d['total'] > threshold, what_changed=changed[:8], observed_cells=len(footprint),
                forecast_issued_at=forecast_result.get('issued_at'), forecast_wind=forecast_result.get('wind'), wind_now=list(sim.wind))


def agent_view(forecast_result, plan='current_orders'):
    """Compact forecast for the agent payload: no cell lists, just what a planner needs."""
    if not forecast_result:
        return None
    summary = forecast_result.get('plans', {}).get(plan) or {}
    return dict(issued_at=forecast_result.get('issued_at'), horizon=forecast_result.get('horizon'), branches=summary.get('branches'),
                basis='Ensemble of reseeded rollouts of the world as currently believed (sensor memory, delayed satellite, smoke report; hidden fire excluded). Probabilities are model-consistent frequencies, not operational forecasts.',
                dispersion=summary.get('dispersion'), expected_burning_cells=summary.get('expected_burning_cells'),
                districts=summary.get('districts'),
                likely_new_fire_cells=[p[:2] for p in summary.get('burn_probability', []) if p[2] >= .5][:40],
                valid=summary.get('valid', True))
