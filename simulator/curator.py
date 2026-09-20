"""Client for the callable 'Experiencia Los Panaderos' workflow (HackSpain folder, hackspainteam9).

Optional and off by default (LP_EXPERIENCE_WORKFLOW=1 enables it). Given the decision payload,
the workflow reads world_state.episode_brief, judges which retrieved precedents apply and returns
the fleet workflow's four mission inputs plus provenance. Dispatcher-supplied fields always win
inside the workflow; when the agent is unsure the deterministic brief is returned unchanged.
Any failure here yields {} so the decision proceeds with the deterministic brief.
"""
import json
import os
import re

WORKFLOW = '01a0bcfb-6ffd-76b6-82db-a13f57c58dd7'
# Persistent id of the 'Resultado de experiencia' Python node whose output is the contract.
RESULT_NODE = '01a0bcfb-7044-7b0f-bf53-4e831e921bf7'
FIELDS = ('mission', 'priority_districts', 'downwind_front', 'tactical_constraints')
REQUEST = ('event_id', 'event_type', 'incident_id', 'sim_time', 'world_state') + FIELDS
ENV_FLAG = 'LP_EXPERIENCE_WORKFLOW'


def enabled():
    return os.environ.get(ENV_FLAG, '').strip().lower() in {'1', 'true', 'yes', 'on'}


def _data(text):
    match = re.search(r'Data:\s*(\{.*)', text, re.S)
    if not match:
        return {}
    try:
        return json.JSONDecoder().raw_decode(match.group(1))[0]
    except ValueError:
        return {}


def curate(robot, payload, timeout=90):
    """Run the workflow; return dict(fields, source, agent_used, confidence, applicable_cases, set_aside, run_id)."""
    request = {k: payload.get(k) or '' for k in REQUEST}
    result = robot.tool('trigger_run', dict(workflow_id=WORKFLOW, environment='development',
                                            payload=json.dumps(request), wait=True), timeout=timeout)
    text = robot.text(result)
    run = re.search(r'Run ID:\s*([0-9a-f-]{36})', text)
    if not run or not re.search(r'Status:\s*completed\b', text):
        raise RuntimeError('Experience workflow run did not complete.')
    run_id = run.group(1)
    listing = robot.tool('monitor_runs', dict(action='outputs', run_id=run_id, node_id=RESULT_NODE))
    output_id = robot.latest_output(robot.text(listing))
    data = _data(robot.text(robot.tool('monitor_runs', dict(action='outputs', run_id=run_id, output_id=output_id))))
    if not isinstance(data, dict) or not all(isinstance(data.get(k), str) for k in FIELDS):
        raise RuntimeError('Experience workflow returned no mission fields.')
    try:
        source = json.loads(data.get('source') or '{}')
    except ValueError:
        source = {}
    try:
        confidence = float(data.get('confidence') or 0)
    except (TypeError, ValueError):
        confidence = 0.
    return dict(fields={k: data[k] for k in FIELDS if data[k].strip()}, source=source,
                agent_used=bool(data.get('agent_used')), confidence=confidence,
                applicable_cases=str(data.get('applicable_cases') or '')[:600],
                set_aside=str(data.get('set_aside') or '')[:600], run_id=run_id)
