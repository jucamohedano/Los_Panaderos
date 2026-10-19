"""Communications policy: who Central warns after each fleet decision, and how.

Two interchangeable policies behind one switch, mirroring the Jev backends in
``reflex.py``::

    COMMS_POLICY=deterministic   (default) rule-based Spanish templates
    COMMS_POLICY=llm             simulator.llm.complete_json, falls back to deterministic

Both return a list of message dicts in the exact shape that
``Simulation.apply_communications`` consumes (``kind``, ``district_id``,
``contact_name``, ``criticality``, ``action``, ``information``, ``status``).
The engine rebuilds each record from a fixed key whitelist, so provenance is
attached out-of-band by ``attach_provenance`` after the engine has applied the
list (the engine returns the very dict objects it stores).

Delivery is SIMULATED. Nothing here sends a Telegram message or places a call.
"""
import json
import os

try:
    from . import contacts, llm
    from .policy import DeterministicFleetPolicy
except ImportError:  # pragma: no cover - flat imports for scripts
    import contacts, llm
    from policy import DeterministicFleetPolicy

# Status stamped on every simulated message. 'sent' is in the engine's
# DELIVERED_STATUSES, so simulated evacuation orders still flip districts to
# 'evacuating' and drive the demo; provenance (policy_source) carries the
# honesty. Set to 'simulated' for zero state effect.
SIMULATED_STATUS = 'sent'

VALID_KINDS = ('zone_alert', 'call', 'personal_message')
VALID_ACTIONS = ('evacuate', 'inform')
SOURCES = ('deterministic', 'llm', 'llm_fallback')
DEFAULT_POLICY = 'deterministic'
MAX_MESSAGES = 12


def refuge_for(group):
    return contacts.REFUGES.get(tuple(group.get('refuge', ())), {})


def person_names():
    return {p['contact_name'] for p in contacts.people()}


def _history(sim, kind, **match):
    """Earlier records of this incident matching all given fields (dedupe source)."""
    return [c for c in sim.communications if c.get('kind') == kind and all(c.get(k) == v for k, v in match.items())]


def observed_state(sim, dispatch=None, event_type='local_observation'):
    """Observation-bounded view given to the LLM. Never reads hidden fire cells."""
    alignment = sim.population_wind_alignment()
    urgent = set(DeterministicFleetPolicy._urgent_districts(sim))
    districts = []
    for key, group in sim.groups.items():
        districts.append(dict(id=key, name=group['name'], kind=group['kind'], population=group['count'],
                              status=group['status'], downwind=bool(alignment[key]['downwind_sector']),
                              urgent=key in urgent, refuge=refuge_for(group).get('name')))
    vehicles = [dict(id=v.get('drone_id') or v.get('truck_id'), role=v.get('role'), mode=v.get('mode'), status=v.get('status'))
                for v in list(sim.scouts) + list(sim.extinguishers) + list(sim.trucks)]
    return dict(tick=sim.tick, event=event_type, wind=list(sim.wind),
                confirmed_fire_cells=len(sim.observation),
                observed_fire_cells=sum(1 for c in sim.memory.values() if c.get('burning')),
                mission=str((dispatch or {}).get('mission', ''))[:300],
                districts=districts, vehicles=vehicles,
                people=[dict(name=p['contact_name'], role=p['role'], district_id=p['district_id']) for p in contacts.people()])


class DeterministicCommsPolicy:
    """Rule-based: evacuate urgent unwarned downwind districts, inform the other
    unwarned districts once a fire is confirmed, call the listed person of each
    district being evacuated. Same input state -> same output, byte for byte."""
    name = 'deterministic'

    def decide(self, sim, dispatch=None, event_type='local_observation'):
        urgent = DeterministicFleetPolicy._urgent_districts(sim)
        out = []
        for key in urgent:
            group = sim.groups[key]
            if group['status'] != 'unwarned':
                continue
            out.append(self.evacuate_message(key, group))
            for person in contacts.people():
                if person['district_id'] == key and not _history(sim, 'call', contact_name=person['contact_name']):
                    out.append(self.call_message(person, group))
        if sim.observation:
            for key, group in sim.groups.items():
                if key in urgent or group['status'] != 'unwarned':
                    continue
                if _history(sim, 'zone_alert', district_id=key, action='inform'):
                    continue
                out.append(self.inform_message(key, group))
        return out[:MAX_MESSAGES]

    @staticmethod
    def evacuate_message(key, group):
        refuge = refuge_for(group)
        text = (f"AVISO DE EVACUACIÓN para {group['name']} ({group['count']} personas): el fuego avanza a sotavento hacia su zona. "
                f"Salgan ahora hacia el punto seguro: {refuge.get('name', 'punto de reunión indicado por la central')}. "
                f"{refuge.get('route', '')}".strip())
        return dict(kind='zone_alert', district_id=key, criticality='alta', action='evacuate',
                    information=text[:600], status=SIMULATED_STATUS)

    @staticmethod
    def inform_message(key, group):
        refuge = refuge_for(group)
        text = (f"INFORMACIÓN para {group['name']} ({group['count']} personas): hay un incendio confirmado en el término municipal. "
                f"No es necesario evacuar por ahora. Permanezcan atentos a este canal; si se ordena la evacuación, "
                f"el punto seguro es {refuge.get('name', 'el indicado por la central')}.")
        return dict(kind='zone_alert', district_id=key, criticality='media', action='inform',
                    information=text[:600], status=SIMULATED_STATUS)

    @staticmethod
    def call_message(person, group):
        refuge = refuge_for(group)
        text = (f"{person['contact_name']}, le llama la central de Los Panaderos. Se ha ordenado la evacuación de {group['name']}. "
                f"Reúna a las personas a su cargo y diríjanse a {refuge.get('name', 'el punto seguro indicado')}. "
                f"{person['mobility']}")
        return dict(kind='call', contact_name=person['contact_name'], district_id=person['district_id'],
                    criticality='alta', information=text[:600], status=SIMULATED_STATUS)


SYSTEM_PROMPT = (
    'Eres la central de comunicaciones de emergencias de Los Panaderos (simulador). '
    'Recibes el estado observado (nunca la verdad oculta del fuego) y devuelves SOLO un objeto JSON '
    '{"messages":[...]} con los avisos a emitir ahora. Cada mensaje: '
    '{"kind":"zone_alert"|"call"|"personal_message","district_id":<id de districts>,'
    '"contact_name":<nombre de people, solo call/personal_message>,"criticality":"alta"|"media"|"baja",'
    '"action":"evacuate"|"inform" (obligatorio en zone_alert),"information":<texto en español, nombra el distrito, '
    'su población y el refugio>}. Reglas: usa solo ids y nombres de la lista; no inventes teléfonos ni chats; '
    'ordena "evacuate" solo a distritos unwarned amenazados a sotavento; "inform" nunca evacúa; '
    'no repitas avisos a distritos que ya no están unwarned; lista vacía si no procede.'
)


class LLMCommsPolicy:
    """Asks the configured model (simulator.llm) for the same message list.
    Any failure or any invalid item falls back to the deterministic policy and
    the records are marked ``policy_source='llm_fallback'``."""
    name = 'llm'

    def __init__(self, fallback=None):
        self.fallback = fallback or DeterministicCommsPolicy()
        self.last_source = None
        self.last_error = None

    def decide(self, sim, dispatch=None, event_type='local_observation'):
        state = observed_state(sim, dispatch, event_type)
        raw = llm.complete_json(SYSTEM_PROMPT, json.dumps(state, ensure_ascii=False)) if llm.configured() else None
        if raw is None:
            return self._fallback(sim, dispatch, event_type, 'no key' if not llm.configured() else 'no response')
        messages, error = validate(raw, sim)
        if error:
            return self._fallback(sim, dispatch, event_type, error)
        self.last_source, self.last_error = 'llm', None
        return messages

    def _fallback(self, sim, dispatch, event_type, why):
        self.last_source, self.last_error = 'llm_fallback', why
        return self.fallback.decide(sim, dispatch, event_type)


def validate(raw, sim):
    """Return (messages, None) when every item is well-formed and names only
    real districts/people; otherwise (None, reason). Whole reply is rejected on
    the first bad item so a hallucinated district never reaches the engine."""
    items = raw.get('messages') if isinstance(raw, dict) else None
    if not isinstance(items, list):
        return None, 'malformed: no messages list'
    names = person_names()
    out = []
    for item in items[:MAX_MESSAGES]:
        if not isinstance(item, dict):
            return None, 'malformed item'
        kind = item.get('kind')
        if kind not in VALID_KINDS:
            return None, f'unknown kind {kind!r}'
        info = str(item.get('information', '')).strip()
        if not info:
            return None, 'empty information'
        crit = str(item.get('criticality') or 'media')[:16]
        if kind == 'zone_alert':
            did = item.get('district_id')
            if did not in sim.groups:
                return None, f'unknown district_id {did!r}'
            action = item.get('action')
            if action not in VALID_ACTIONS:
                return None, f'missing/unknown action {action!r}'
            out.append(dict(kind=kind, district_id=did, criticality=crit, action=action, information=info[:600], status=SIMULATED_STATUS))
        else:
            name = str(item.get('contact_name', '')).strip()
            if name not in names:
                return None, f'unknown contact {name!r}'
            did = item.get('district_id')
            out.append(dict(kind=kind, contact_name=name, district_id=did if did in sim.groups else None,
                            criticality=crit, information=info[:600], status=SIMULATED_STATUS))
    return out, None


def source_of(pol):
    """Provenance label for the records a policy just produced."""
    if isinstance(pol, LLMCommsPolicy):
        return pol.last_source or 'llm_fallback'
    return 'deterministic'


def attach_provenance(applied, source):
    """Stamp ``policy_source`` on the records the engine returned. Requires the
    caller to have sent only VALID_KINDS dicts so nothing was skipped."""
    for rec in applied:
        rec['policy_source'] = source
    return applied


def policy_name(value=None):
    value = (os.environ.get('COMMS_POLICY', DEFAULT_POLICY) if value is None else value).strip().lower()
    return value if value in ('deterministic', 'llm') else DEFAULT_POLICY


def policy(value=None):
    """Factory: ``COMMS_POLICY=llm`` -> LLMCommsPolicy, anything else -> deterministic."""
    return LLMCommsPolicy() if policy_name(value) == 'llm' else DeterministicCommsPolicy()
