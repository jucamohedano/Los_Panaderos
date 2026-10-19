"""Post-mortem reflection: decision record + telemetry + oracle -> diagnosis.

``reflect`` speaks the retired 'Post-mortem Los Panaderos' workflow protocol
through a duck-typed ``robot`` and is kept for recorded-run replays.
``reflect_local`` asks a model directly through ``llm.complete_json`` and
returns None when no key is configured.
"""
import json
import re

from . import llm

REFLECTION_WORKFLOW = '01a0bbc7-be65-7370-89ce-e367801031fc'
# Persistent id of the 'Diagnostico' Python node whose output carries the diagnosis.
DIAGNOSIS_NODE = '01a0bbc7-beab-70ce-a5ef-d8cdcf601f90'
REQUIRED = ('gap_type', 'root_cause', 'proposed_rule', 'confidence')
FIELDS = ('gap_type', 'root_cause', 'evidence_step_indexes', 'prompt_section', 'proposed_rule', 'proposed_prompt_patch', 'confidence')
MAX_CHARS = 24000


def _trim(items, budget):
    text = json.dumps(items, ensure_ascii=False)
    while len(text) > budget and items:
        items = [dict(i, reasoning=str(i.get('reasoning', ''))[:400], arguments=str(i.get('arguments', ''))[:300])
                 for i in items][:max(1, len(items)-2)]
        text = json.dumps(items, ensure_ascii=False)
    return text


def build_payload(record, steps, signals, evaluation, prompt_excerpt=''):
    rec = {k: record.get(k) for k in ('id', 'tick', 'event_type', 'status', 'reject_reason', 'latency_s', 'run_id')}
    rec['decision'] = json.loads(record.get('decision_json') or '{}')
    rec['outcome'] = json.loads(record.get('outcome_json') or 'null')
    ev = dict(evaluation or {})
    ev.pop('weights', None)
    return dict(decision_record=json.dumps(rec, ensure_ascii=False)[:MAX_CHARS],
                telemetry_steps=_trim([dict(index=i, **s) for i, s in enumerate(steps)], MAX_CHARS),
                signals=json.dumps(signals or {}, ensure_ascii=False),
                oracle_result=json.dumps(ev, ensure_ascii=False)[:MAX_CHARS],
                prompt_excerpt=str(prompt_excerpt or '')[:4000])


def _data(text):
    match = re.search(r'Data:\s*(\{.*)', text, re.S)
    if not match:
        return {}
    try:
        return json.JSONDecoder().raw_decode(match.group(1))[0]
    except ValueError:
        return {}


def _diagnosis(data):
    if isinstance(data.get('response'), dict):
        data = data['response']
    if not isinstance(data, dict) or not all(k in data for k in REQUIRED):
        raise RuntimeError('Reflection returned no structured diagnosis.')
    diagnosis = {k: data.get(k) for k in FIELDS}
    try:
        diagnosis['confidence'] = float(diagnosis['confidence'] or 0)
    except (TypeError, ValueError):
        diagnosis['confidence'] = 0.
    if isinstance(diagnosis['evidence_step_indexes'], str):
        try:
            diagnosis['evidence_step_indexes'] = json.loads(diagnosis['evidence_step_indexes'])
        except ValueError:
            diagnosis['evidence_step_indexes'] = []
    return str(data.get('reflection', ''))[:4000], diagnosis


SYSTEM_PROMPT = (
    'You audit one decision of a wildfire dispatch policy. You receive the frozen decision record, the '
    'reasoning steps recorded during the decision, automatic signals and a hindsight oracle result. '
    'Reply with a single JSON object with keys: reflection (short Spanish paragraph), gap_type (one of '
    'execution, judgement, information, none), root_cause, evidence_step_indexes (list of ints), '
    'prompt_section, proposed_rule (one imperative sentence or empty), proposed_prompt_patch (or empty), '
    'confidence (0-1).')


def reflect_local(payload, timeout=120):
    """Diagnose through the configured model; None when ``llm`` has no key."""
    data = llm.complete_json(SYSTEM_PROMPT, json.dumps(payload, ensure_ascii=False), timeout)
    if data is None:
        return None
    text, diagnosis = _diagnosis(data)
    return text, diagnosis, None


def reflect(robot, payload, timeout=240):
    """Run the reflection workflow and return (reflection text, diagnosis, run id)."""
    result = robot.tool('trigger_run', dict(workflow_id=REFLECTION_WORKFLOW, environment='development',
                                            payload=json.dumps(payload), wait=True), timeout=timeout)
    text = robot.text(result)
    run = re.search(r'Run ID:\s*([0-9a-f-]{36})', text)
    if not run or not re.search(r'Status:\s*completed\b', text):
        raise RuntimeError('Reflection run did not complete.')
    run_id = run.group(1)
    listing = robot.tool('monitor_runs', dict(action='outputs', run_id=run_id, node_id=DIAGNOSIS_NODE))
    output_id = robot.latest_output(robot.text(listing))
    data = _data(robot.text(robot.tool('monitor_runs', dict(action='outputs', run_id=run_id, output_id=output_id))))
    text, diagnosis = _diagnosis(data)
    return text, diagnosis, run_id
