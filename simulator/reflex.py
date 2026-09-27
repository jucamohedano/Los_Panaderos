"""Jev reflex (System One) in shadow mode: a typed fast decision recorded next to HappyRobot's, never applied.

Jev (`typesafe/jev-1.13` via the OpenRouter decisions API) answers typed questions over a compact
belief state: one `choice` per vehicle among the simulator's own validated candidates
(`oracle.vehicle_options`), a `noul` "this needs Central" and a `score` for threat. Code owns the
loop: the candidate list is enumerated here, the assembled decision is validated with `sim.apply`
on a copy, and a routing verdict says whether a gated cascade *would* have applied it or escalated.

Shadow only. The controller records the verdict in the black box and the oracle grades it with the
same hindsight cost as the real decision, so the dashboard can show whether the reflex would have
helped before anyone lets it act. Off without OPENROUTER_API_KEY or with REFLEX_MODE=off.
"""
import copy
import json
import math
import os
from pathlib import Path
import time
import urllib.request

from . import experience, oracle

MODEL = 'typesafe/jev-1.13'
URL = 'https://openrouter.ai/api/alpha/decisions'
ENV_KEY = 'OPENROUTER_API_KEY'
ENV_MODE = 'REFLEX_MODE'
TIMEOUT = 3.0
CONFIDENCE = .7          # below this a gated cascade escalates to Central
ESCALATE = .5            # Jev's own "needs Central" noul at or above this escalates
HIGH_STAKES = ('evacuate_farm', 'evacuate_town', 'attack_sector', 'contain')
THREAT = ('calm', 'watch', 'urgent', 'critical')
ENV_FILE = Path(__file__).resolve().parents[1] / '.env'


def api_key():
    return os.environ.get(ENV_KEY, '').strip()


def load_env_key():
    """Export OPENROUTER_API_KEY from the gitignored repo .env when the server starts; never logged."""
    if api_key() or not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text(encoding='utf-8').splitlines():
        if line.strip().startswith(ENV_KEY+'='):
            os.environ[ENV_KEY] = line.split('=', 1)[1].strip().strip('"').strip("'")
            return


def mode():
    value = os.environ.get(ENV_MODE, '').strip().lower()
    if value in ('off', 'shadow'):
        return value
    return 'shadow' if api_key() else 'off'


def enabled():
    return mode() != 'off' and bool(api_key())


def describe(sim, event_type='local_observation', brief=None):
    """Compact belief state for Jev: no hidden truth, no full grid, only what the first minutes need."""
    sig = experience.signature(sim, event_type)
    state = dict(
        event=event_type, tick=sim.tick,
        wind=dict(direction=experience.wind_direction(sim.wind) if sig['wind_strength'] else 'calm', strength=sig['wind_strength']),
        fire=dict(confirmed=sig['fire_confirmed'], believed_cells=sig['believed_fire_cells'], seen_now_cells=len(sim.observation),
                  smoke_report=list(sim.report) if sim.called else None),
        districts={k: dict(status=d['status'], people=d['people'], distance_cells=d['distance'], downwind=d['downwind'] >= .5)
                   for k, d in sig['districts'].items()},
        vehicles={**{v['drone_id']: dict(role='extinguisher', status=v['status']) for v in sim.extinguishers},
                  **{v['drone_id']: dict(role='scout', status=v['status']) for v in sim.scouts},
                  **{v['truck_id']: dict(role='truck', status=v['status']) for v in sim.trucks}},
        labels=sig['labels'],
    )
    if brief:
        state['priority_districts'] = brief.get('priority_districts', [])[:3]
        if brief.get('cases'):
            c = brief['cases'][0]
            state['similar_case'] = dict(did=c.get('did'), regret=c.get('regret'), better=c.get('oracle_preferred'))
        if brief.get('lessons'):
            state['lessons'] = [l.get('rule') for l in brief['lessons'][:3]]
    return state


def _label(order):
    cmd = order['command']
    if 'target_x' in order and cmd not in ('hold', 'continue'):
        return f"{cmd} {order['target_x']},{order['target_y']}"
    if order.get('district_id'):
        return f"{cmd} {order['district_id']}"
    return cmd


def _describe_option(order):
    cmd = order['command']
    text = {'hold': 'stay where it is', 'continue': 'keep the current task', 'scout': 'fly to the smoke report to confirm the fire',
            'contain': 'drop retardant at a safe containment position ahead of the front', 'patrol': 'patrol a ring around the report',
            'attack_sector': 'drive to the leading edge and attack it', 'evacuate_farm': 'warn and evacuate the farm',
            'evacuate_town': 'warn and evacuate the district'}.get(cmd, cmd)
    if order.get('district_id'):
        text += f" ({order['district_id']})"
    elif 'target_x' in order and cmd not in ('hold', 'continue'):
        text += f" at {order['target_x']},{order['target_y']}"
    return text


def questions(options):
    """One typed choice per vehicle plus the two fixed questions; single-option vehicles are resolved in code."""
    qs, fixed = {}, {}
    for key, opts in options:
        vid = opts[0].get('drone_id') or opts[0].get('truck_id')
        labels = {}
        for o in opts:
            labels.setdefault(_label(o), o)
        if len(labels) == 1:
            fixed[vid] = (key, next(iter(labels.values())))
            continue
        qs[vid] = dict(type='choice', instructions=f'Order for {vid} in the first minutes of a wildfire; people first, then containment.',
                       criteria={lab: _describe_option(o) for lab, o in labels.items()})
    qs['escalate'] = dict(type='noul', instructions='This situation needs the central coordinator (HappyRobot) rather than a reflex: '
                                                    'contradictory evidence, an evacuation to decide, or nothing routine.')
    qs['threat'] = dict(type='score', instructions='How urgent is the threat to people right now?', criteria=list(THREAT))
    return qs, fixed


def _post(body, key, timeout):
    req = urllib.request.Request(URL, data=json.dumps(body).encode(), method='POST',
                                 headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def ask(state, qs, key, timeout=TIMEOUT):
    return _post(dict(model=MODEL, state=state, questions=qs), key, timeout)


def _command_confidence(answer, lookup, choice):
    """Probability mass on the chosen *kind* of order (e.g. any of the three scout cells), not the exact cell."""
    probs = answer.get('probabilities')
    if isinstance(probs, dict) and probs:
        cmd = lookup[choice]['command']
        mass = sum(float(p) for lab, p in probs.items() if lab in lookup and lookup[lab]['command'] == cmd and isinstance(p, (int, float)))
        return max(0., min(1., mass))
    return float(answer.get('confidence', 0.) or 0.)


def assemble(answers, options, qs, fixed):
    """Map Jev's per-vehicle choices back onto the enumerated orders; unknown choices fall back to the passive default."""
    parts, confidences, unknown = [], [], []
    labels = {}
    for key, opts in options:
        vid = opts[0].get('drone_id') or opts[0].get('truck_id')
        labels[vid] = (key, {_label(o): o for o in opts})
    for vid, (key, lookup) in labels.items():
        if vid in fixed:
            parts.append((key, fixed[vid][1]))
            continue
        ans = answers.get(vid) or {}
        choice = ans.get('choice')
        if choice in lookup:
            parts.append((key, lookup[choice]))
            confidences.append(_command_confidence(ans, lookup, choice))
        else:
            parts.append((key, next(iter(lookup.values()))))
            unknown.append(vid)
            confidences.append(0.)
    decision = oracle.assemble(parts)
    decision.update(mission='jev reflex (shadow)', drone_reason='typed reflex choice among validated candidates')
    return decision, (min(confidences) if confidences else 1.), unknown


def route(decision, confidence, escalate, unknown, valid):
    reasons = []
    if not valid:
        reasons.append('assembled decision failed validation')
    if unknown:
        reasons.append(f"unknown choice for {', '.join(unknown)}")
    if escalate is not None and escalate >= ESCALATE:
        reasons.append(f'Jev asked for Central ({escalate:.2f})')
    if confidence < CONFIDENCE:
        reasons.append(f'confidence {confidence:.2f} below {CONFIDENCE}')
    orders = decision.get('extinguisher_orders', [])+decision.get('scout_orders', [])+decision.get('truck_orders', [])
    stakes = sorted({o['command'] for o in orders if o['command'] in HIGH_STAKES})
    if stakes:
        reasons.append(f"high stakes: {', '.join(stakes)}")
    return ('central' if reasons else 'reflex'), reasons


def summarize(decision):
    orders = decision.get('extinguisher_orders', [])+decision.get('scout_orders', [])+decision.get('truck_orders', [])
    return [f"{o.get('drone_id') or o.get('truck_id')}: {_label(o)}" for o in orders]


def decide(sim, event_type='local_observation', brief=None, key=None, timeout=TIMEOUT):
    """Shadow verdict for the frozen world: what Jev would order, how sure it is, and whether a cascade would let it act."""
    key = key or api_key()
    if not key:
        raise RuntimeError('reflex disabled: no key')
    start = time.monotonic()
    options = oracle.vehicle_options(sim)
    if not options:
        raise RuntimeError('reflex skipped: no vehicles')
    state = describe(sim, event_type, brief)
    qs, fixed = questions(options)
    raw = ask(state, qs, key, timeout)
    answers = raw.get('answers') or {}
    decision, confidence, unknown = assemble(answers, options, qs, fixed)
    valid = oracle.is_valid(copy.deepcopy(sim), decision)
    escalate = answers.get('escalate', {}).get('noul')
    escalate = float(escalate) if isinstance(escalate, (int, float)) and math.isfinite(escalate) else None
    threat = answers.get('threat', {})
    verdict, reasons = route(decision, confidence, escalate, unknown, valid)
    return dict(mode='shadow', model=raw.get('model', MODEL), route=verdict, reasons=reasons, decision=decision, orders=summarize(decision),
                confidence=round(confidence, 3), escalate=escalate, valid=valid,
                threat=dict(score=threat.get('score'), label=THREAT[min(len(THREAT)-1, max(0, int(round(threat['score']))))] if isinstance(threat.get('score'), (int, float)) else None),
                latency_ms=int((time.monotonic()-start)*1000), questions=len(qs), state_chars=len(json.dumps(state)))


def agreement(reflex_decision, actual):
    """Share of vehicles for which Jev's order equals the applied one (by command and target)."""
    mine = dict(o.split(': ', 1) for o in summarize(reflex_decision))
    theirs = dict(o.split(': ', 1) for o in summarize(actual)) if actual else {}
    if not mine:
        return None
    return round(sum(theirs.get(v) == c for v, c in mine.items())/len(mine), 2)


def grade(snapshot, verdict, evaluation):
    """Hindsight cost of the shadow decision with the oracle's seeds/horizon; regret against the oracle's best."""
    if not verdict.get('valid') or evaluation.get('best_cost') is None:
        return None
    sim = copy.deepcopy(snapshot)
    seeds, horizon = evaluation.get('seeds') or [9], evaluation.get('horizon', 16)
    cost = round(sum(oracle.rollout(sim, verdict['decision'], horizon, s) for s in seeds)/len(seeds), 3)
    return dict(cost=cost, regret=round(cost-evaluation['best_cost'], 3))
