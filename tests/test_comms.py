"""Offline tests for simulator.comms. No network: llm.complete_json is patched or unconfigured."""
import copy
import os
import unittest
from unittest.mock import patch

from simulator import comms, contacts
from simulator.engine import Simulation
from simulator.session import SimulatorSession

NO_KEY = {k: v for k, v in os.environ.items() if k not in comms.llm.KEY_VARS}


def incident(wind=(-3, 0)):
    sim = Simulation(drone_count=2)
    sim.ignite();sim.farmer_call();sim.set_wind(x=wind[0], y=wind[1])
    return sim


class DeterministicCommsTests(unittest.TestCase):
    def setUp(self):
        self.policy = comms.DeterministicCommsPolicy()

    def test_warns_urgent_unwarned_downwind_district_with_explicit_action(self):
        sim = incident()
        urgent = comms.DeterministicFleetPolicy._urgent_districts(sim)
        self.assertTrue(urgent)
        msgs = self.policy.decide(sim, {}, 'farmer_call')
        alerts = {m['district_id']: m for m in msgs if m['kind'] == 'zone_alert'}
        self.assertEqual(set(alerts), set(urgent))
        for m in msgs:
            self.assertIn(m['kind'], comms.VALID_KINDS)
            self.assertEqual(m['status'], comms.SIMULATED_STATUS)
            if m['kind'] == 'zone_alert':
                self.assertEqual(m['action'], 'evacuate')
                self.assertIn(m['district_id'], sim.groups)
                group = sim.groups[m['district_id']]
                self.assertIn(group['name'], m['information'])
                self.assertIn(str(group['count']), m['information'])
                self.assertIn(contacts.REFUGES[tuple(group['refuge'])]['name'], m['information'])
        applied = sim.apply_communications(msgs)
        self.assertEqual(len(applied), len(msgs))
        for key in urgent:
            self.assertEqual(sim.groups[key]['status'], 'evacuating')
        self.assertEqual(sim.pending_decision_event, 'evacuation_warning_delivered')

    def test_calls_only_listed_people_of_evacuated_districts(self):
        sim = incident()
        msgs = self.policy.decide(sim, {}, 'farmer_call')
        calls = [m for m in msgs if m['kind'] == 'call']
        names = comms.person_names()
        self.assertTrue(calls)
        for c in calls:
            self.assertIn(c['contact_name'], names)
            self.assertIn(c['district_id'], {m['district_id'] for m in msgs if m.get('action') == 'evacuate'})

    def test_calm_wind_emits_no_evacuation(self):
        sim = incident(wind=(1, 0))
        msgs = self.policy.decide(sim, {}, 'farmer_call')
        self.assertFalse([m for m in msgs if m.get('action') == 'evacuate'])

    def test_inform_never_evacuates(self):
        sim = incident(wind=(1, 0))
        sim.drone.update(x=69., y=43.);sim.observe()
        self.assertTrue(sim.observation)
        msgs = self.policy.decide(sim, {}, 'scout_fire_confirmation')
        informs = [m for m in msgs if m['kind'] == 'zone_alert']
        self.assertTrue(informs)
        self.assertTrue(all(m['action'] == 'inform' for m in informs))
        before = {k: g['status'] for k, g in sim.groups.items()}
        applied = sim.apply_communications(msgs)
        self.assertTrue(all(r['effect'] == 'district_informed' and r['action_source'] == 'explicit' for r in applied))
        self.assertEqual(before, {k: g['status'] for k, g in sim.groups.items()})
        self.assertIsNone(sim.pending_decision_event)
        # Informs are not repeated on the next decision for the same incident.
        self.assertEqual([m for m in self.policy.decide(sim, {}, 'local_observation') if m['kind'] == 'zone_alert'], [])

    def test_identical_state_yields_identical_output_and_leaves_state_untouched(self):
        sim = incident()
        before = copy.deepcopy(sim.state())
        self.assertEqual(self.policy.decide(sim, {}, 'x'), self.policy.decide(sim, {}, 'x'))
        self.assertEqual(before, sim.state())
        other = incident()
        self.assertEqual(self.policy.decide(sim, {}, 'x'), self.policy.decide(other, {}, 'x'))

    def test_never_reads_hidden_fire_truth(self):
        sim = incident(wind=(1, 0))
        with open(comms.__file__, encoding='utf-8') as fh:src = fh.read()
        self.assertNotIn('.cells', src)
        self.assertNotIn('burning_cells', src)
        # observation-bounded state mentions only observed/confirmed counts
        state = comms.observed_state(sim)
        self.assertEqual(set(state) & {'cells', 'ground_truth'}, set())
        self.assertEqual(state['confirmed_fire_cells'], len(sim.observation))


class SwitchTests(unittest.TestCase):
    def test_default_and_unknown_values_are_deterministic(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('COMMS_POLICY', None)
            self.assertIsInstance(comms.policy(), comms.DeterministicCommsPolicy)
        with patch.dict(os.environ, {'COMMS_POLICY': 'quantum'}):
            self.assertIsInstance(comms.policy(), comms.DeterministicCommsPolicy)
            self.assertEqual(comms.policy_name(), 'deterministic')
        self.assertIsInstance(comms.policy(''), comms.DeterministicCommsPolicy)

    def test_llm_value_selects_llm_policy(self):
        with patch.dict(os.environ, {'COMMS_POLICY': 'LLM'}):
            self.assertIsInstance(comms.policy(), comms.LLMCommsPolicy)


class LLMCommsTests(unittest.TestCase):
    def test_no_key_falls_back_and_marks_llm_fallback(self):
        sim = incident()
        pol = comms.LLMCommsPolicy()
        with patch.dict(os.environ, NO_KEY, clear=True), patch.object(comms.llm, '_env_file_value', return_value=None), \
                patch.object(comms.llm, 'complete') as complete:
            msgs = pol.decide(sim, {}, 'farmer_call')
        complete.assert_not_called()
        self.assertEqual(msgs, comms.DeterministicCommsPolicy().decide(sim, {}, 'farmer_call'))
        self.assertEqual(comms.source_of(pol), 'llm_fallback')
        self.assertEqual(pol.last_error, 'no key')

    def test_none_reply_falls_back(self):
        sim = incident()
        pol = comms.LLMCommsPolicy()
        with patch.object(comms.llm, 'configured', return_value=True), patch.object(comms.llm, 'complete_json', return_value=None):
            msgs = pol.decide(sim, {}, 'x')
        self.assertEqual(comms.source_of(pol), 'llm_fallback')
        self.assertTrue(msgs)

    def test_unknown_district_id_is_rejected_not_accepted(self):
        sim = incident()
        reply = dict(messages=[dict(kind='zone_alert', district_id='atlantis', action='evacuate', criticality='alta', information='Evacuar Atlantis')])
        pol = comms.LLMCommsPolicy()
        with patch.object(comms.llm, 'configured', return_value=True), patch.object(comms.llm, 'complete_json', return_value=reply):
            msgs = pol.decide(sim, {}, 'x')
        self.assertFalse(any(m.get('district_id') == 'atlantis' for m in msgs))
        self.assertEqual(comms.source_of(pol), 'llm_fallback')
        self.assertIn('atlantis', pol.last_error)

    def test_unknown_contact_or_missing_action_is_rejected(self):
        sim = incident()
        for bad in (dict(kind='call', contact_name='Nadie Inventado', information='hola'),
                    dict(kind='zone_alert', district_id='town', information='sin action'),
                    dict(kind='zone_alert', district_id='town', action='panic', information='x'),
                    dict(kind='sms', district_id='town', action='inform', information='x'),
                    dict(kind='zone_alert', district_id='town', action='inform', information='')):
            msgs, err = comms.validate(dict(messages=[bad]), sim)
            self.assertIsNone(msgs, bad);self.assertTrue(err, bad)
        self.assertEqual(comms.validate(dict(nope=1), sim), (None, 'malformed: no messages list'))

    def test_valid_reply_is_accepted_and_marked_llm(self):
        sim = incident(wind=(1, 0))
        reply = dict(messages=[
            dict(kind='zone_alert', district_id='town', action='inform', criticality='media', information='Casco Histórico: sin evacuar.'),
            dict(kind='personal_message', contact_name='Carmen Ortega', district_id='town_north', criticality='baja', information='Aviso preventivo.'),
            dict(kind='zone_alert', district_id='farm', action='evacuate', criticality='alta', information='Evacuar la granja.')])
        pol = comms.LLMCommsPolicy()
        with patch.object(comms.llm, 'configured', return_value=True), patch.object(comms.llm, 'complete_json', return_value=reply) as cj:
            msgs = pol.decide(sim, {'mission': 'm'}, 'x')
        self.assertEqual(comms.source_of(pol), 'llm')
        self.assertEqual([m['kind'] for m in msgs], ['zone_alert', 'personal_message', 'zone_alert'])
        self.assertTrue(all(m['status'] == comms.SIMULATED_STATUS for m in msgs))
        system, user = cj.call_args[0][:2]
        self.assertNotIn('"cells"', user);self.assertNotIn('heat', user);self.assertIn('"downwind"', user);self.assertIn('"refuge"', user)
        applied = comms.attach_provenance(sim.apply_communications(msgs), comms.source_of(pol))
        self.assertEqual(sim.groups['farm']['status'], 'evacuating')
        self.assertEqual(sim.groups['town']['status'], 'unwarned')
        self.assertTrue(all(r['policy_source'] == 'llm' for r in sim.communications[-3:]))


class SessionWiringTests(unittest.TestCase):
    def run_session(self):
        with patch.dict(os.environ, {'COMMS_POLICY': 'deterministic'}):
            s = SimulatorSession(now_ms=0)
        s.action('ignite', now_ms=0);s.sim.set_wind(x=-3, y=0);s.action('call', now_ms=0)
        return s

    def test_dispatch_carries_real_avisos_and_recipients_with_provenance(self):
        s = self.run_session()
        d = s.sim.dispatch
        self.assertTrue(d['avisos_lanzados']);self.assertTrue(d['destinatarios'])
        self.assertEqual(len(d['avisos_lanzados']), len(s.sim.communications))
        self.assertTrue(all('[deterministic]' in a for a in d['avisos_lanzados']))
        names = {g['name'] for g in s.sim.groups.values()} | comms.person_names()
        self.assertTrue(set(d['destinatarios']) <= names)
        # provenance landed in the engine's stored history (same dict objects), survives checkpoint and public state
        self.assertTrue(all(c['policy_source'] == 'deterministic' for c in s.sim.communications))
        restored = SimulatorSession.restore(s.checkpoint())
        self.assertTrue(all(c['policy_source'] == 'deterministic' for c in restored.sim.communications))
        self.assertEqual(s.public_state()['comms_policy'], 'deterministic')
        self.assertTrue(s.decision_log[-1]['communications'])
        self.assertTrue(any(g['status'] == 'evacuating' for g in s.sim.groups.values()))

    def test_comms_failure_never_breaks_the_decision(self):
        class Boom:
            name = 'boom'
            def decide(self, sim, dispatch, event):raise RuntimeError('comms crashed')
        s = SimulatorSession(now_ms=0);s.comms = Boom()
        s.action('ignite', now_ms=0);state = s.action('call', now_ms=0)
        self.assertEqual(state['decisions'][-1]['status'], 'accepted')
        self.assertEqual(s.sim.dispatch['avisos_lanzados'], [])
        self.assertTrue(any('comms crashed' in e['message'] for e in state['history']))

    def test_llm_mode_without_key_marks_fallback_in_session(self):
        with patch.dict(os.environ, dict(NO_KEY, COMMS_POLICY='llm'), clear=True), patch.object(comms.llm, '_env_file_value', return_value=None):
            s = SimulatorSession(now_ms=0)
            s.action('ignite', now_ms=0);s.sim.set_wind(x=-3, y=0);s.action('call', now_ms=0)
        self.assertEqual(s.public_state()['comms_policy'], 'llm')
        self.assertTrue(s.sim.communications)
        self.assertTrue(all(c['policy_source'] == 'llm_fallback' for c in s.sim.communications))
        self.assertTrue(all('[llm_fallback]' in a for a in s.sim.dispatch['avisos_lanzados']))


if __name__ == '__main__':
    unittest.main()
