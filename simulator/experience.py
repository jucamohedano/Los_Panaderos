"""Experience replay for HappyRobot decisions: case memory, retrieval, lesson credit.

The black box already freezes every decision with the world the agent saw, what it
ordered, what the hindsight oracle would have done and the regret between them. This
module turns those rows into experience the *next* decision can use. The policy is a
frozen language model, so nothing is trained: the k most similar past situations are
retrieved and handed to the agent in the payload (`world_state.similar_cases`) with
the action taken, the oracle's best alternative and the regret, so it can reason
"last time this looked like this, X cost us Y".

Similarity is defined on a small, interpretable *situation signature* built from the
belief world only (wind, believed fire, per-district exposure, fleet availability,
minutes since the alarm). The weights favour what matters in the first minutes of a
wildfire: who is unwarned downwind and what is idle.

Lessons get the same treatment: each carries the signature it was learned in, is
retrieved by relevance rather than recency, and is credited with the regret of the
decisions it was shown to. A lesson that does not lower regret is retired.
"""
import json
import math

from . import worlds

SIGNATURE_WEIGHTS = dict(phase=.10, wind=.15, fire=.15, districts=.45, fleet=.15)
DISTRICT_WEIGHTS = dict(status=.35, downwind=.35, distance=.30)
EARLY_TICKS = 30          # the "first minutes"; decisions inside this window compare by fine-grained phase
FAR_CELLS = 40            # district distance beyond which "how far" stops mattering
NEAR_CELLS = worlds.THREAT_CELLS
DEFAULT_K = 3
MAX_CASE_DISTANCE = .25   # cases further than this are not shown: unrelated experience is noise
LESSON_CONFIDENCE = .5    # tier-1 lessons below this are logged but never enter a payload
LESSON_MIN_USES = 3       # credit assignment needs this many shown decisions before judging a lesson
LESSON_RETIRE_MARGIN = 10.  # retire when decisions shown the lesson regret this much more than those without


def _mean(values):
    values = [v for v in values if v is not None]
    return round(sum(values)/len(values), 3) if values else None


def signature(sim, event_type='local_observation'):
    """Compact description of the situation as the agent sees it (no hidden fire)."""
    belief = worlds.belief_world(sim)
    fire = belief.fire_points()
    sources = fire or ([tuple(sim.report)] if sim.called else [])
    strength = math.hypot(*sim.wind)
    districts = {}
    for key, g in sim.groups.items():
        dist, cos = FAR_CELLS, 0.
        for x, y in sources:
            dx, dy = g['x']-x, g['y']-y
            d = math.hypot(dx, dy)
            if d < dist:
                dist = d
                cos = (dx*sim.wind[0]+dy*sim.wind[1])/(d*strength) if d and strength else 0.
        districts[key] = dict(status=g['status'], distance=round(min(dist, FAR_CELLS), 1), downwind=round(max(0., cos), 2), people=g['count'])
    idle = {'at_station', 'awaiting_assignment', 'holding'}
    fleet = dict(scouts=len(sim.scouts), extinguishers=len(sim.extinguishers), trucks=len(sim.trucks),
                 idle_scouts=sum(v['status'] in idle for v in sim.scouts),
                 idle_extinguishers=sum(v['status'] in idle for v in sim.extinguishers),
                 trucks_mobile=sum(v['status'] not in ('at_station', 'mobilizing') for v in sim.trucks))
    return dict(tick=sim.tick, event_type=event_type, wind=[sim.wind[0], sim.wind[1]], wind_strength=round(strength, 2),
                believed_fire_cells=len(fire), fire_confirmed=bool(fire), fire_centroid=[round(sum(p[0] for p in fire)/len(fire), 1), round(sum(p[1] for p in fire)/len(fire), 1)] if fire else None,
                districts=districts, fleet=fleet)


def _angle(a, b):
    sa, sb = math.hypot(*a), math.hypot(*b)
    if not sa or not sb:
        return 0. if sa == sb else 1.
    return math.acos(max(-1., min(1., (a[0]*b[0]+a[1]*b[1])/(sa*sb))))/math.pi


def signature_distance(a, b, weights=SIGNATURE_WEIGHTS):
    """0 = same situation, 1 = nothing in common; every term is explainable."""
    terms = {}
    ta, tb = min(a['tick'], EARLY_TICKS), min(b['tick'], EARLY_TICKS)
    terms['phase'] = abs(ta-tb)/EARLY_TICKS
    terms['wind'] = .6*_angle(a['wind'], b['wind'])+.4*min(1., abs(a['wind_strength']-b['wind_strength'])/4.)
    fa, fb = a['believed_fire_cells'], b['believed_fire_cells']
    terms['fire'] = .5*(a['fire_confirmed'] != b['fire_confirmed'])+.5*(abs(fa-fb)/max(fa, fb) if max(fa, fb) else 0.)
    keys = set(a['districts']) | set(b['districts'])
    per = []
    for k in keys:
        da, db = a['districts'].get(k), b['districts'].get(k)
        if not da or not db:
            per.append(1.)
            continue
        status = abs(worlds.PEOPLE_RANK.get(da['status'], 2)-worlds.PEOPLE_RANK.get(db['status'], 2))/4
        per.append(DISTRICT_WEIGHTS['status']*status+DISTRICT_WEIGHTS['downwind']*abs(da['downwind']-db['downwind'])
                   +DISTRICT_WEIGHTS['distance']*abs(da['distance']-db['distance'])/FAR_CELLS)
    terms['districts'] = sum(per)/len(per) if per else 0.
    fla, flb = a['fleet'], b['fleet']
    terms['fleet'] = sum(min(1., abs(fla.get(k, 0)-flb.get(k, 0))/3) for k in ('idle_scouts', 'idle_extinguishers', 'trucks_mobile'))/3
    total = sum(weights[k]*v for k, v in terms.items())
    return dict(total=round(min(1., total), 4), terms={k: round(v, 4) for k, v in terms.items()})


def summarise_orders(decision):
    """One line per vehicle: what was actually ordered."""
    out = []
    for o in (decision or {}).get('extinguisher_orders') or []:
        tgt = f" @({o.get('target_x')},{o.get('target_y')})" if o.get('command') in ('contain', 'scout') else (f" {o.get('district_id')}" if o.get('district_id') else '')
        out.append(f"{o.get('drone_id')}: {o.get('command')}{tgt}")
    for o in (decision or {}).get('scout_orders') or []:
        tgt = f" {o.get('district_id')}" if o.get('district_id') else (f" via {o.get('waypoints')}" if o.get('command') == 'patrol' else '')
        out.append(f"{o.get('drone_id')}: {o.get('command')}{tgt}")
    for o in (decision or {}).get('truck_orders') or []:
        tgt = f" @({o.get('target_x')},{o.get('target_y')})" if o.get('target_x') is not None else ''
        out.append(f"{o.get('truck_id')}: {o.get('command')}{tgt}")
    return out


def _load(raw):
    return json.loads(raw) if raw else None


def case_view(row, match):
    """What the agent gets to see about one past decision."""
    evaluation, outcome, diagnosis, sig = _load(row.get('result_json')), _load(row.get('outcome_json')), _load(row.get('diagnosis_json')), _load(row.get('signature_json'))
    decision = _load(row.get('decision_json')) or {}
    return dict(decision_id=row['id'], incident_id=row['incident_id'], similarity_distance=match['total'], why_similar=match['terms'],
                tick=row['tick'], event_type=row['event_type'], wind=sig['wind'] if sig else None,
                situation={k: dict(status=v['status'], distance=v['distance'], downwind=v['downwind']) for k, v in (sig or {}).get('districts', {}).items()},
                did=summarise_orders(decision), mission=decision.get('mission'),
                outcome=dict(people_changes=outcome.get('people_changes'), newly_burned_cells=outcome.get('newly_burned_cells'), people_burnt=outcome.get('people_burnt')) if outcome else None,
                regret=evaluation.get('regret') if evaluation else None, gap_type=evaluation.get('gap_type') if evaluation else None,
                oracle_preferred=summarise_orders(evaluation.get('best_decision')) if evaluation and evaluation.get('regret') else None,
                root_cause=(diagnosis or {}).get('root_cause'), lesson=(diagnosis or {}).get('proposed_rule'))


def retrieve(box, sig, k=DEFAULT_K, exclude_incident=None, max_distance=MAX_CASE_DISTANCE):
    """The k closest graded past decisions, nearest first."""
    scored = []
    for row in box.cases(exclude_incident):
        other = _load(row.get('signature_json'))
        if not other:
            continue
        match = signature_distance(sig, other)
        if match['total'] <= max_distance:
            scored.append((match['total'], -(row['id']), row, match))
    scored.sort(key=lambda t: (t[0], t[1]))
    return [case_view(row, match) for _, _, row, match in scored[:k]]


def relevant_lessons(box, sig, limit=5):
    """Active lessons ordered by how close their learning situation is to this one; context-free lessons last."""
    rows = box.lesson_rows()
    scored = []
    for r in rows:
        ctx = _load(r.get('context_json'))
        d = signature_distance(sig, ctx)['total'] if ctx and sig else 1.
        scored.append((d, -r['id'], r))
    scored.sort(key=lambda t: (t[0], t[1]))
    return [dict(id=r['id'], rule=r['rule'], relevance=round(1-d, 3)) for d, _, r in scored[:limit]]


def credit(box, lesson_id):
    """Regret of decisions that were shown the lesson versus those that were not."""
    shown, hidden = box.lesson_regrets(lesson_id)
    return dict(uses=len(shown), regret_with=_mean(shown), regret_without=_mean(hidden))


def review_lessons(box):
    """Retire lessons that have been shown often enough and made things worse. Returns the retired ids."""
    retired = []
    for r in box.lesson_rows():
        c = credit(box, r['id'])
        box.set_lesson_credit(r['id'], c)
        if c['uses'] >= LESSON_MIN_USES and c['regret_with'] is not None and c['regret_without'] is not None \
                and c['regret_with'] > c['regret_without']+LESSON_RETIRE_MARGIN:
            box.retire_lesson(r['id'], f"regret with lesson {c['regret_with']} vs without {c['regret_without']} over {c['uses']} decisions")
            retired.append(r['id'])
    return retired


def learning_curve(box):
    """Per-incident aggregates: is regret falling as experience accumulates?"""
    episodes = []
    for ep in box.episodes():
        regrets = [r for r in ep['regrets'] if r is not None]
        episodes.append(dict(incident_id=ep['incident_id'], started_at=ep['started_at'], decisions=ep['decisions'], graded=len(regrets),
                             mean_regret=_mean(regrets), total_regret=round(sum(regrets), 3) if regrets else None,
                             judgement_gaps=ep['judgement_gaps'], execution_gaps=ep['execution_gaps'],
                             surprise_checks=ep['surprise_checks'], divergences=ep['divergences'], mean_surprise=ep['mean_surprise'],
                             people_burnt=ep['people_burnt'], cases_available=ep['cases_before'], lessons_shown=ep['lessons_shown']))
    return episodes
