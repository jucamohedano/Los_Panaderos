"""Post-decision pipeline: telemetry -> oracle -> reflection -> healing.

Runs in a background thread after a decision is applied. Every stage is
isolated: a failure is logged into the incident history as a 'system' entry and
the remaining stages that do not depend on it still run.

``robot`` is the duck-typed platform client. Without one, the stages that read
run telemetry or trigger the reflection workflow fall back to an
``llm.PlatformStub`` (recorded, never sent) and reflection is attempted through
``reflection.reflect_local``, which is a no-op when no model key is configured.
"""
import json

from . import experience, llm, oracle, reflection, reflex, telemetry
from .healing import Healer


class Analyst:
    def __init__(self, box, robot=None, set_lessons=lambda lessons: None, log=lambda *a, **k: None,
                 workflow_id=llm.WORKFLOW, root=llm.ROOT):
        self.robot = robot if robot is not None else llm.PlatformStub()
        self.box, self.set_lessons, self.log = box, set_lessons, log
        self.healer = Healer(box, self.robot, workflow_id, root)

    def _reflect(self, payload):
        if isinstance(self.robot, llm.PlatformStub):
            local = reflection.reflect_local(payload)
            if local is None:
                raise RuntimeError('no reflection backend: HappyRobot retired and LLM_API_KEY unset')
            return local
        return reflection.reflect(self.robot, payload)

    def analyse(self, decision_id, payload, run_id, listing):
        out = dict(decision_id=decision_id)
        steps, signals = [], {}
        try:
            steps = telemetry.harvest(self.robot, run_id, listing or None) if run_id else []
            signals = telemetry.signals(steps, payload)
            self.box.save_telemetry(decision_id, steps, signals)
            out['signals'] = signals
        except Exception as exc:
            out['telemetry_error'] = str(exc)[:300]
            self.log('system', f'Telemetry unavailable for decision {decision_id}: {exc}'[:300])
        row = self.box.decision(decision_id)
        if row is None:
            return out
        evaluation = dict(gap_type='unknown', regret=None)
        try:
            snapshot = self.box.load_snapshot(decision_id)
            if snapshot is not None:
                evaluation = oracle.evaluate(snapshot, json.loads(row['decision_json'] or '{}'), signals)
                if row['status'] != 'applied':
                    evaluation['gap_type'] = 'execution'
            self.box.save_evaluation(decision_id, evaluation)
            out['evaluation'] = evaluation
            shadow = self.box.reflex(decision_id)
            if shadow and snapshot is not None:
                grade = reflex.grade(snapshot, shadow, evaluation)
                if grade and evaluation.get('actual_cost') is not None:
                    grade['vs_central'] = round(grade['cost']-evaluation['actual_cost'], 3)
                self.box.set_reflex_regret(decision_id, grade)
                out['reflex'] = grade
            retired = experience.review_lessons(self.box)
            if retired:
                out['retired_lessons'] = retired
                self.log('post-mortem', f"Retired lesson(s) {retired}: decisions shown them regretted more than those without.")
                self.set_lessons(self.box.active_lessons(5))
        except Exception as exc:
            out['oracle_error'] = str(exc)[:300]
            self.log('system', f'Oracle failed for decision {decision_id}: {exc}'[:300])
        try:
            text, diagnosis, rid = self._reflect(reflection.build_payload(row, steps, signals, evaluation))
            self.box.save_reflection(decision_id, rid, text, diagnosis)
            out.update(reflection=text, diagnosis=diagnosis, reflection_run_id=rid)
            self.log('post-mortem', text[:600], gap=diagnosis.get('gap_type'), regret=evaluation.get('regret'), decision_id=decision_id)
        except Exception as exc:
            out['reflection_error'] = str(exc)[:300]
            self.log('system', f'Reflection unavailable for decision {decision_id}: {exc}'[:300])
            return out
        try:
            out['healing'] = self.healer.apply(decision_id, run_id, diagnosis, evaluation, steps)
            self.set_lessons(self.box.active_lessons(5))
        except Exception as exc:
            out['healing_error'] = str(exc)[:300]
            self.log('system', f'Healing failed for decision {decision_id}: {exc}'[:300])
        return out
