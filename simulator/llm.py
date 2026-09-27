"""Local replacement for the retired HappyRobot client.

The HappyRobot workflows (Central dispatch, post-mortem reflection, prompt
patching) are no longer reachable and the MCP client was removed upstream.
This module keeps the adaptation stack importable and runnable without them:

* ``complete`` calls an OpenAI-compatible chat model when ``LLM_API_KEY`` is set
  and returns ``None`` otherwise, so every caller degrades to a no-op.
* ``PlatformStub`` implements the duck-typed ``tool``/``text``/``latest_output``
  interface the healing and telemetry code used against the MCP client. It never
  performs I/O: every call is recorded in ``sent`` and answered with empty
  content, which the callers already treat as "nothing happened".
* ``decisions``, ``normalize`` and ``latest_output`` are the pure parsers that
  used to live on ``HappyRobot``; they only read text/JSON already on disk.
"""
import json
import os
import re
from pathlib import Path
from urllib import error, request

ROOT = Path(__file__).resolve().parents[1]
# Id of the retired 'Los Panaderos' workflow. Kept so recorded stub calls stay
# comparable with the archived run evidence; nothing is sent to it.
WORKFLOW = '01a0b8ea-d9af-71f3-9fb7-8a469f9ac25b'

DEFAULT_BASE_URL = 'https://api.openai.com/v1'
DEFAULT_MODEL = 'gpt-4o-mini'


def configured():
    return bool(os.environ.get('LLM_API_KEY'))


def complete(system, user, timeout=60):
    """Return the model's reply text, or None when no key is configured or the call fails."""
    key = os.environ.get('LLM_API_KEY')
    if not key:
        return None
    body = json.dumps(dict(model=os.environ.get('LLM_MODEL', DEFAULT_MODEL),
                           messages=[dict(role='system', content=system), dict(role='user', content=user)],
                           temperature=0)).encode()
    url = os.environ.get('LLM_BASE_URL', DEFAULT_BASE_URL).rstrip('/')+'/chat/completions'
    req = request.Request(url, data=body, headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'})
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
        return data['choices'][0]['message']['content']
    except (error.URLError, OSError, KeyError, IndexError, ValueError, TypeError):
        return None


def complete_json(system, user, timeout=60):
    """Like ``complete`` but parse the first JSON object in the reply; None when unavailable."""
    text = complete(system, user, timeout)
    if not text:
        return None
    start = text.find('{')
    if start < 0:
        return None
    try:
        parsed, _ = json.JSONDecoder().raw_decode(text[start:])
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


# Keys that mark a mission output from 'Resultado de la mision' (v24+).
ORDER_KEYS = {'primary_command', 'extinguisher_orders', 'scout_orders', 'truck_orders'}
# Keys that mark a flat single-drone decision from the earlier shape.
FLAT_KEYS = {'command', 'target_x', 'target_y', 'reason', 'mission'}
# Coordinate fields that the platform encoded as strings.
INT_FIELDS = ('target_x', 'target_y', 'truck_target_x', 'truck_target_y')


def _coerce_ints(mapping):
    """Accept canonical integers only, without rounding or coercing garbage."""
    for field in INT_FIELDS:
        value = mapping.get(field)
        if isinstance(value, str) and re.fullmatch(r'-?\d{1,4}', value):
            mapping[field] = int(value)


def decisions(value):
    """Extract structured mission output from MCP JSON/text wrappers, never guess."""
    found = []
    if isinstance(value, dict):
        if FLAT_KEYS <= value.keys() or ORDER_KEYS <= value.keys():
            found.append(value)
        else:
            for v in value.values():
                found.extend(decisions(v))
    elif isinstance(value, list):
        for v in value:
            found.extend(decisions(v))
    elif isinstance(value, str):
        decoder = json.JSONDecoder()
        pos = 0
        while pos < len(value):
            indexes = [i for i in (value.find('{', pos), value.find('[', pos)) if i >= 0]
            if not indexes:
                break
            start = min(indexes)
            try:
                parsed, end = decoder.raw_decode(value[start:])
                found.extend(decisions(parsed))
                pos = start+end
            except json.JSONDecodeError:
                pos = start+1
    return found


def normalize(decision):
    """Coerce coordinates anywhere the mission output carries them."""
    decision = dict(decision)
    _coerce_ints(decision)
    for key in ('extinguisher_orders', 'scout_orders', 'truck_orders'):
        orders = decision.get(key)
        if isinstance(orders, str):
            try:
                orders = json.loads(orders)
            except (ValueError, TypeError):
                continue
            decision[key] = orders
        if isinstance(orders, list):
            for order in orders:
                if isinstance(order, dict):
                    _coerce_ints(order)
    return decision


def latest_output(listing):
    """Return the id of the final successful output block of a run listing."""
    outputs = []
    for block in listing.split('## '):
        oid = re.search(r'Output ID:\s*([0-9a-f-]{36})', block)
        ts = re.search(r'Timestamp:\s*(\S+)', block)
        if oid and ts:
            outputs.append((ts.group(1), oid.group(1), bool(re.search(r'Status:\s*succeeded\b', block))))
    if not outputs:
        raise RuntimeError('No delegated drone output was returned; no command applied.')
    latest = max(outputs)
    if not latest[2]:
        raise RuntimeError('The final drone decision failed; no command applied.')
    return latest[1]


class PlatformStub:
    """No-op stand-in for the MCP client.

    ``tool`` records ``(name, arguments)`` in ``sent`` and returns an empty
    result. Callers that parse run ids or output ids out of the text therefore
    fail closed ("run did not complete", "no output") exactly as they did when
    the platform was down, and the higher layers log that and move on.
    """

    def __init__(self):
        self.sent = []
        self.last_run_id = None
        self.last_listing = None

    def tool(self, name, arguments, timeout=60):
        self.sent.append(dict(tool=name, arguments=arguments))
        return {'content': [{'text': ''}]}

    def close(self):
        pass

    @staticmethod
    def text(result):
        return '\n'.join(c.get('text', '') for c in result.get('content', []))

    decisions = staticmethod(decisions)
    normalize = staticmethod(normalize)
    latest_output = staticmethod(latest_output)
