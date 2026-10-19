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

Lessons carry the signature they were learned in and are ranked by relevance
rather than recency. Exposed/unexposed regret can trigger retirement, but that
observational comparison does not establish whether the lesson helped.
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
LABEL_VERSION = 2          # 2: downwind is measured for every district, not only those within FAR_CELLS
BRIEF_VERSION = 1
BRIEF_CASES = 3            # cases in the decision-time brief; more is noise for a one-shot agent
BRIEF_TEXT_CHARS = 1200    # hard cap on the prompt-ready paragraph
BRIEF_FIELD_CHARS = 160    # hard cap on any free-text field copied from a past case
BRIEF_ROLE = ('Evidence, not orders: graded past decisions, lessons and the possible-worlds forecast for this situation. '
              'Current observations, rules and constraints prevail; say which evidence you used or set aside.')
DIRECTIONS = ('east', 'southeast', 'south', 'southwest', 'west', 'northwest', 'north', 'northeast')


def _mean(values):
    values = [v for v in values if v is not None]
    return round(sum(values)/len(values), 3) if values else None


def signature(sim, event_type='local_observation'):
    """Compact description of the situation as the agent sees it (no hidden fire)."""
    belief = worlds.belief_world(sim)
    fire = belief.fire_points()
    observed = [(cell['x'], cell['y']) for cell in sim.memory.values() if cell['burning']]
    sources = fire or ([tuple(sim.report)] if sim.called else [])
    strength = math.hypot(*sim.wind)
    districts = {}
    for key, g in sim.groups.items():
        dist, cos = math.inf, 0.
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
    sig = dict(tick=sim.tick, event_type=event_type, wind=[sim.wind[0], sim.wind[1]], wind_strength=round(strength, 2),
               believed_fire_cells=len(fire), observed_fire_cells=len(observed), fire_confirmed=bool(observed),
               fire_centroid=[round(sum(p[0] for p in observed)/len(observed), 1), round(sum(p[1] for p in observed)/len(observed), 1)] if observed else None,
               districts=districts, fleet=fleet)
    return dict(sig, label_version=LABEL_VERSION, labels=situation_labels(sig))


def wind_direction(wind):
    """Compass name of where the wind pushes the fire (vector points TO spread)."""
    return DIRECTIONS[math.floor(math.atan2(wind[1], wind[0])/(math.pi/4)+.5) % 8]


def situation_labels(sig):
    """Version-one labels derived solely from the stored belief signature."""
    labels = ['phase:early' if sig['tick'] < EARLY_TICKS else 'phase:later',
              f"event:{sig['event_type']}", 'fire:observed' if sig['fire_confirmed'] else 'fire:unconfirmed']
    if not sig['wind_strength']:
        labels.append('wind:calm')
    else:
        labels.extend([f'wind:{wind_direction(sig["wind"])}', 'wind:strong' if sig['wind_strength'] >= 2 else 'wind:light'])
    unwarned = [d for d in sig['districts'].values() if d['status'] == 'unwarned']
    labels.append('people:downwind' if any(d['downwind'] >= .5 for d in unwarned) else 'people:no_downwind')
    if any(d['distance'] <= NEAR_CELLS for d in unwarned):
        labels.append('people:nearby')
    labels.extend(f"fleet:{role}:{sig['fleet'][role]}" for role in ('scouts', 'extinguishers', 'trucks'))
    return labels


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
    composition = sum(min(1., abs(fla.get(k, 0)-flb.get(k, 0))/2) for k in ('scouts', 'extinguishers', 'trucks'))/3
    availability = sum(min(1., abs(fla.get(k, 0)-flb.get(k, 0))/3) for k in ('idle_scouts', 'idle_extinguishers', 'trucks_mobile'))/3
    terms['fleet'] = .5*composition+.5*availability
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


def case_view(row, match, current_signature):
    """What the agent gets to see about one past decision."""
    evaluation, outcome, diagnosis, sig = _load(row.get('result_json')), _load(row.get('outcome_json')), _load(row.get('diagnosis_json')), _load(row.get('signature_json'))
    decision = _load(row.get('decision_json')) or {}
    labels, current_labels = situation_labels(sig), situation_labels(current_signature)
    return dict(decision_id=row['id'], incident_id=row['incident_id'], similarity_distance=match['total'], why_similar=match['terms'],
                label_version=LABEL_VERSION, labels=labels,
                matching_labels=[label for label in labels if label in current_labels],
                differing_labels=dict(current_only=[label for label in current_labels if label not in labels],
                                      case_only=[label for label in labels if label not in current_labels]),
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
        evaluation = _load(row.get('result_json')) or {}
        regret = evaluation.get('regret')
        if not other or evaluation.get('truncated') or isinstance(regret, bool) \
                or not isinstance(regret, (int, float)) or not math.isfinite(regret):
            continue
        match = signature_distance(sig, other)
        if match['total'] <= max_distance:
            scored.append((match['total'], -(row['id']), row, match))
    scored.sort(key=lambda t: (t[0], t[1]))
    return [case_view(row, match, sig) for _, _, row, match in scored[:k]]


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


def _clip(text, limit=BRIEF_FIELD_CHARS):
    if not isinstance(text, str):
        return None
    text = ' '.join(text.split())
    return text if len(text) <= limit else text[:limit-1].rstrip()+'…'


def rank_districts(sig, forecast_view=None):
    """Exposed unwarned districts, people x max(forecast threat, downwind, proximity) first; unexposed ones are left out."""
    threat = (forecast_view or {}).get('districts') or {}
    ranked = []
    for key, d in sig['districts'].items():
        if d['status'] != 'unwarned':
            continue
        p_fire = (threat.get(key) or {}).get('p_fire_within_8')
        exposure = max(p_fire if p_fire is not None else 0., d['downwind'], 1. if d['distance'] <= NEAR_CELLS else 0.)
        why = []
        if p_fire is not None and p_fire >= .5:
            why.append(f'fire within {NEAR_CELLS} cells in {int(round(p_fire*100))}% of possible worlds')
        if d['downwind'] >= .5:
            why.append('downwind of the believed fire')
        if d['distance'] <= NEAR_CELLS:
            why.append(f"{d['distance']} cells from the believed fire")
        if not why:
            continue
        ranked.append(dict(district_id=key, status=d['status'], people=d.get('people'), p_fire_within_8=p_fire,
                           downwind=d['downwind'], distance=d['distance'], exposure=round(exposure, 2), why=why))
    ranked.sort(key=lambda r: (-(r['people'] or 0)*r['exposure'], -r['exposure'], r['distance'], r['district_id']))
    return ranked


def describe_front(sig):
    """Where the fire is being pushed, from belief only."""
    if not sig['wind_strength']:
        base = 'calm wind: no preferred front, spread is roughly radial'
    else:
        base = f"wind pushes the fire {wind_direction(sig['wind'])} ({sig['wind'][0]},{sig['wind'][1]}): work the {wind_direction(sig['wind'])}-facing leading edge"
    if sig['fire_confirmed'] and sig['fire_centroid']:
        count = sig.get('observed_fire_cells', sig['believed_fire_cells'])
        return f"{base}; {count} burning cells in sensor memory around ({sig['fire_centroid'][0]},{sig['fire_centroid'][1]})"
    return f'{base}; fire not yet observed by local sensors; forecast fire is inferred from reports'


def brief(sig, cases, lessons, forecast_view=None, divergence=None):
    """Bounded, auditable decision-time context: what the agent may learn from, and what it is not."""
    priority = rank_districts(sig, forecast_view)
    compact_cases = []
    for c in (cases or [])[:BRIEF_CASES]:
        compact_cases.append(dict(decision_id=c['decision_id'], similarity_distance=c['similarity_distance'],
                                  matching_labels=len(c['matching_labels']), differs=(c['differing_labels']['current_only']+c['differing_labels']['case_only'])[:6],
                                  did=[_clip(d, 60) for d in c['did'][:4]], regret=c['regret'], gap_type=c['gap_type'],
                                  oracle_preferred=[_clip(d, 60) for d in (c['oracle_preferred'] or [])[:4]] or None,
                                  root_cause=_clip(c['root_cause']), lesson=_clip(c['lesson'])))
    compact_lessons = [dict(id=l['id'], rule=_clip(l['rule']), relevance=l['relevance']) for l in (lessons or [])[:5]]
    fc = None
    if forecast_view:
        fc = dict(horizon=forecast_view.get('horizon'), branches=forecast_view.get('branches'), dispersion=forecast_view.get('dispersion'),
                  expected_burning_cells=forecast_view.get('expected_burning_cells'),
                  threatened=[k for k, v in (forecast_view.get('districts') or {}).items() if (v.get('p_fire_within_8') or 0) >= .5])
    dv = None
    if divergence and divergence.get('divergent'):
        dv = dict(distance=divergence.get('distance'), threshold=divergence.get('threshold'), what_changed=(divergence.get('what_changed') or [])[:4])
    out = dict(version=BRIEF_VERSION, role=BRIEF_ROLE,
               situation=dict(tick=sig['tick'], event_type=sig['event_type'], labels=sig.get('labels') or situation_labels(sig),
                              unwarned=sorted(k for k, d in sig['districts'].items() if d['status'] == 'unwarned')),
               priority_districts=priority, downwind_front=describe_front(sig),
               cases=compact_cases, lessons=compact_lessons, forecast=fc, divergence=dv)
    out['text'] = brief_text(out)
    return out


def brief_text(b):
    """One prompt-ready paragraph, hard-capped; the structured brief is the source of truth."""
    parts = []
    if b['priority_districts']:
        parts.append('Priority (evidence): '+'; '.join(f"{r['district_id']} ({r['people']} people, {r['why'][0]})" for r in b['priority_districts'][:3])+'.')
    elif b['situation']['unwarned']:
        parts.append('Unwarned but not exposed yet: '+', '.join(b['situation']['unwarned'])+'.')
    else:
        parts.append('No unwarned districts remain.')
    parts.append('Front: '+b['downwind_front']+'.')
    if b['divergence']:
        parts.append('Forecast broke: '+'; '.join(b['divergence']['what_changed'])+'. Revise the affected assignments first.')
    for c in b['cases']:
        line = f"Case {c['decision_id']} (distance {c['similarity_distance']}): did {', '.join(c['did']) or 'nothing recorded'}; regret {c['regret']}"
        if c['oracle_preferred']:
            line += f"; hindsight preferred {', '.join(c['oracle_preferred'])}"
        if c['root_cause']:
            line += f"; cause: {c['root_cause']}"
        if c['differs']:
            line += f"; differs now: {', '.join(c['differs'])}"
        parts.append(line+'.')
    if b['lessons']:
        parts.append('Lessons: '+' | '.join(l['rule'] for l in b['lessons'][:3]))
    closing = 'Cases and lessons are evidence, not orders.'
    budget, kept = BRIEF_TEXT_CHARS-len(closing)-1, []
    for part in parts:
        if len(part) >= budget:
            part = part[:budget-2].rstrip()+'…'
        budget -= len(part)+1
        kept.append(part)
        if budget <= 1:
            break
    return ' '.join(kept+[closing])


def dispatch_fields(b):
    """The four mission inputs of the fleet workflow, filled from the brief when no dispatcher supplied them."""
    top = b['priority_districts'][:3]
    if top:
        mission = 'Protect exposed unwarned people first: '+', '.join(f"{r['district_id']} ({r['why'][0]})" for r in top)+'. Then contain the leading downwind front from a validated safe position.'
    elif b['situation']['unwarned']:
        mission = 'No district is exposed yet: contain the leading downwind front from a validated safe position and keep observing the approaches to '+', '.join(b['situation']['unwarned'][:3])+'.'
    else:
        mission = 'All districts warned or beyond warning: contain the leading downwind front from a validated safe position and keep observing.'
    constraints = b['text']
    return dict(mission=mission, priority_districts=json.dumps([r['district_id'] for r in top]),
                downwind_front=b['downwind_front'], tactical_constraints=constraints)


def credit(box, lesson_id):
    """Regret of decisions that were shown the lesson versus those that were not."""
    shown, hidden = box.lesson_regrets(lesson_id)
    return dict(uses=len(shown), regret_with=_mean(shown), regret_without=_mean(hidden))


def review_lessons(box):
    """Retire lessons whose exposed mean regret exceeds the comparison margin."""
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
