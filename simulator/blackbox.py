"""SQLite flight recorder for HappyRobot decisions.

Stores the frozen world the agent saw, its decision, platform telemetry, oracle
grades and reflections. Snapshots are pickled Simulation objects so the oracle can
restore and replay them.
"""
import copy
import json
import pickle
import sqlite3
import threading
import time
import zlib
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions(id INTEGER PRIMARY KEY, incident_id TEXT, tick INTEGER, event_type TEXT,
  created_at REAL, payload_json TEXT, snapshot_blob BLOB, run_id TEXT, latency_s REAL, status TEXT,
  decision_json TEXT, reject_reason TEXT, metrics_json TEXT, outcome_json TEXT);
CREATE TABLE IF NOT EXISTS telemetry(decision_id INTEGER, agent TEXT, step_index INTEGER, timestamp TEXT,
  reasoning TEXT, action TEXT, arguments_json TEXT, outcome_json TEXT);
CREATE TABLE IF NOT EXISTS signals(decision_id INTEGER PRIMARY KEY, signals_json TEXT);
CREATE TABLE IF NOT EXISTS evaluations(decision_id INTEGER PRIMARY KEY, result_json TEXT);
CREATE TABLE IF NOT EXISTS reflections(decision_id INTEGER PRIMARY KEY, run_id TEXT, text TEXT, diagnosis_json TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS lessons(id INTEGER PRIMARY KEY, rule TEXT, norm TEXT UNIQUE, source_decision_id INTEGER, created_at REAL, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS patches(id INTEGER PRIMARY KEY, decision_id INTEGER, version_id TEXT, report_path TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS forecasts(decision_id INTEGER, kind TEXT, issued_at INTEGER, forecast_json TEXT, PRIMARY KEY(decision_id, kind));
CREATE TABLE IF NOT EXISTS surprises(id INTEGER PRIMARY KEY, decision_id INTEGER, tick INTEGER, distance REAL, threshold REAL, divergent INTEGER, surprise_json TEXT);
CREATE TABLE IF NOT EXISTS cases(decision_id INTEGER PRIMARY KEY, incident_id TEXT, signature_json TEXT);
CREATE TABLE IF NOT EXISTS lesson_uses(decision_id INTEGER, lesson_id INTEGER, PRIMARY KEY(decision_id, lesson_id));
CREATE TABLE IF NOT EXISTS reflexes(decision_id INTEGER PRIMARY KEY, mode TEXT, route TEXT, confidence REAL, agreement REAL, latency_ms INTEGER, regret REAL, reflex_json TEXT);
"""
# Columns added to `lessons` after the first release; applied idempotently to old databases.
LESSON_COLUMNS = dict(context_json='TEXT', gap_type='TEXT', confidence='REAL', uses='INTEGER DEFAULT 0',
                      regret_with='REAL', regret_without='REAL', retired_reason='TEXT')


class BlackBox:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(self.path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with self.lock:
            self.db.executescript(SCHEMA)
            present = {r['name'] for r in self.db.execute('PRAGMA table_info(lessons)')}
            for name, kind in LESSON_COLUMNS.items():
                if name not in present:
                    self.db.execute(f'ALTER TABLE lessons ADD COLUMN {name} {kind}')
            self.db.commit()

    def close(self):
        with self.lock:
            self.db.close()

    @staticmethod
    def snapshot_metrics(sim):
        return dict(tick=sim.tick,
                    burning=sum(sim.burning(c) for row in sim.cells for c in row),
                    burned=sum(c.get('burned', 0) > 0 for row in sim.cells for c in row),
                    burnt_people=sum(g.get('burnt', 0) for g in sim.groups.values()),
                    drone_extinguished=sim.suppressed, truck_extinguished=sim.crew_extinguished,
                    people={k: g['status'] for k, g in sim.groups.items()})

    def record_decision(self, sim, payload, run_id, latency, decision, status, reject_reason=''):
        try:
            blob = zlib.compress(pickle.dumps(copy.deepcopy(sim), protocol=pickle.HIGHEST_PROTOCOL))
        except Exception:
            blob = None
        with self.lock:
            cur = self.db.execute(
                'INSERT INTO decisions(incident_id,tick,event_type,created_at,payload_json,snapshot_blob,run_id,latency_s,status,decision_json,reject_reason,metrics_json) '
                'VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (sim.incident_id, sim.tick, payload.get('event_type'), time.time(), json.dumps(payload), blob, run_id, latency,
                 status, json.dumps(decision), reject_reason, json.dumps(self.snapshot_metrics(sim))))
            self.db.commit()
            return cur.lastrowid

    def set_status(self, decision_id, status, reject_reason=''):
        with self.lock:
            self.db.execute('UPDATE decisions SET status=?, reject_reason=? WHERE id=?', (status, reject_reason, decision_id))
            self.db.commit()

    def finish_outcome(self, decision_id, sim):
        row = self.decision(decision_id)
        if row is None:
            return None
        before = json.loads(row['metrics_json'])
        after = self.snapshot_metrics(sim)
        out = dict(elapsed_steps=after['tick']-before['tick'],
                   newly_burned_cells=after['burned']-before['burned'],
                   people_burnt=after['burnt_people']-before['burnt_people'],
                   drone_extinguished=after['drone_extinguished']-before['drone_extinguished'],
                   truck_extinguished=after['truck_extinguished']-before['truck_extinguished'],
                   people=after['people'],
                   people_changes={k: [before['people'].get(k), v] for k, v in after['people'].items() if before['people'].get(k) != v})
        with self.lock:
            self.db.execute('UPDATE decisions SET outcome_json=? WHERE id=?', (json.dumps(out), decision_id))
            self.db.commit()
        return out

    def load_snapshot(self, decision_id):
        row = self.decision(decision_id)
        if row is None or row['snapshot_blob'] is None:
            return None
        return pickle.loads(zlib.decompress(row['snapshot_blob']))

    def decision(self, decision_id):
        with self.lock:
            row = self.db.execute('SELECT * FROM decisions WHERE id=?', (decision_id,)).fetchone()
        return dict(row) if row else None

    def list_decisions(self, incident_id):
        with self.lock:
            rows = self.db.execute(
                'SELECT d.id,d.tick,d.event_type,d.created_at,d.run_id,d.latency_s,d.status,d.decision_json,d.reject_reason,d.outcome_json,'
                's.signals_json,e.result_json,r.text AS reflection,r.diagnosis_json,r.run_id AS reflection_run_id,x.reflex_json FROM decisions d '
                'LEFT JOIN signals s ON s.decision_id=d.id LEFT JOIN evaluations e ON e.decision_id=d.id '
                'LEFT JOIN reflections r ON r.decision_id=d.id LEFT JOIN reflexes x ON x.decision_id=d.id WHERE d.incident_id=? ORDER BY d.id', (incident_id,)).fetchall()
        return [dict(r) for r in rows]

    def save_reflex(self, decision_id, verdict, agreement=None):
        """Shadow reflex verdict recorded beside the real decision; graded later by ``set_reflex_regret``."""
        with self.lock:
            self.db.execute('INSERT OR REPLACE INTO reflexes(decision_id,mode,route,confidence,agreement,latency_ms,regret,reflex_json) VALUES(?,?,?,?,?,?,NULL,?)',
                            (decision_id, verdict.get('mode'), verdict.get('route'), verdict.get('confidence'), agreement, verdict.get('latency_ms'),
                             json.dumps(dict(verdict, agreement=agreement))))
            self.db.commit()

    def reflex(self, decision_id):
        with self.lock:
            row = self.db.execute('SELECT reflex_json FROM reflexes WHERE decision_id=?', (decision_id,)).fetchone()
        return json.loads(row['reflex_json']) if row else None

    def set_reflex_regret(self, decision_id, grade):
        with self.lock:
            row = self.db.execute('SELECT reflex_json FROM reflexes WHERE decision_id=?', (decision_id,)).fetchone()
            if row is None:
                return
            data = dict(json.loads(row['reflex_json']), grade=grade)
            self.db.execute('UPDATE reflexes SET regret=?, reflex_json=? WHERE decision_id=?', (grade.get('regret') if grade else None, json.dumps(data), decision_id))
            self.db.commit()

    def reflex_summary(self):
        """Shadow record across all incidents: how often Jev would have acted, agreed and what it would have cost."""
        with self.lock:
            rows = self.db.execute('SELECT x.route,x.confidence,x.agreement,x.latency_ms,x.regret,e.result_json FROM reflexes x '
                                   'LEFT JOIN evaluations e ON e.decision_id=x.decision_id').fetchall()
        if not rows:
            return None
        graded = [(r['regret'], json.loads(r['result_json']).get('regret')) for r in rows if r['regret'] is not None and r['result_json']]
        graded = [(a, b) for a, b in graded if b is not None]
        return dict(decisions=len(rows), would_act=sum(r['route'] == 'reflex' for r in rows),
                    mean_confidence=round(sum(r['confidence'] or 0 for r in rows)/len(rows), 2),
                    mean_agreement=round(sum(r['agreement'] for r in rows if r['agreement'] is not None)/max(1, sum(r['agreement'] is not None for r in rows)), 2),
                    mean_latency_ms=int(sum(r['latency_ms'] or 0 for r in rows)/len(rows)), graded=len(graded),
                    reflex_mean_regret=round(sum(a for a, _ in graded)/len(graded), 1) if graded else None,
                    central_mean_regret=round(sum(b for _, b in graded)/len(graded), 1) if graded else None,
                    reflex_better=sum(a < b for a, b in graded), reflex_worse=sum(a > b for a, b in graded))

    def save_telemetry(self, decision_id, steps, signals):
        with self.lock:
            self.db.execute('DELETE FROM telemetry WHERE decision_id=?', (decision_id,))
            self.db.executemany('INSERT INTO telemetry VALUES(?,?,?,?,?,?,?,?)',
                [(decision_id, s['agent'], i, s['timestamp'], s['reasoning'], s['action'], json.dumps(s['arguments']), json.dumps(s['outcome']))
                 for i, s in enumerate(steps)])
            self.db.execute('INSERT OR REPLACE INTO signals VALUES(?,?)', (decision_id, json.dumps(signals)))
            self.db.commit()

    def telemetry(self, decision_id):
        with self.lock:
            rows = self.db.execute('SELECT * FROM telemetry WHERE decision_id=? ORDER BY step_index', (decision_id,)).fetchall()
        return [dict(r, arguments=json.loads(r['arguments_json']), outcome=json.loads(r['outcome_json'])) for r in rows]

    def evaluation(self, decision_id):
        with self.lock:
            row = self.db.execute('SELECT result_json FROM evaluations WHERE decision_id=?', (decision_id,)).fetchone()
        return json.loads(row['result_json']) if row else None

    def save_evaluation(self, decision_id, result):
        with self.lock:
            self.db.execute('INSERT OR REPLACE INTO evaluations VALUES(?,?)', (decision_id, json.dumps(result)))
            self.db.commit()

    def save_reflection(self, decision_id, run_id, text, diagnosis):
        with self.lock:
            self.db.execute('INSERT OR REPLACE INTO reflections VALUES(?,?,?,?,?)', (decision_id, run_id, text, json.dumps(diagnosis), time.time()))
            self.db.commit()

    def diagnoses(self):
        with self.lock:
            rows = self.db.execute('SELECT diagnosis_json FROM reflections').fetchall()
        return [json.loads(r['diagnosis_json']) for r in rows]

    def add_lesson(self, rule, decision_id, context=None, gap_type=None, confidence=None):
        norm = ' '.join(str(rule).lower().split())
        if not norm:
            return None
        with self.lock:
            existing = self.db.execute('SELECT id FROM lessons WHERE norm=?', (norm,)).fetchone()
            if existing:
                self.db.execute('UPDATE lessons SET rule=?, confidence=MAX(COALESCE(confidence,0),?), context_json=COALESCE(context_json,?) WHERE id=?',
                                (str(rule).strip(), confidence or 0., json.dumps(context) if context else None, existing['id']))
                self.db.commit()
                return existing['id']
            cur = self.db.execute('INSERT INTO lessons(rule,norm,source_decision_id,created_at,active,context_json,gap_type,confidence,uses) VALUES(?,?,?,?,1,?,?,?,0)',
                                  (str(rule).strip(), norm, decision_id, time.time(), json.dumps(context) if context else None, gap_type, confidence))
            self.db.commit()
            return cur.lastrowid

    def active_lessons(self, limit=5):
        with self.lock:
            rows = self.db.execute('SELECT rule FROM lessons WHERE active=1 ORDER BY id DESC LIMIT ?', (limit,)).fetchall()
        return [r['rule'] for r in rows]

    def lesson_rows(self, active_only=True):
        with self.lock:
            rows = self.db.execute('SELECT * FROM lessons'+(' WHERE active=1' if active_only else '')+' ORDER BY id DESC').fetchall()
        return [dict(r) for r in rows]

    def lesson_ids_for(self, rules):
        norms = [' '.join(str(r).lower().split()) for r in rules or []]
        if not norms:
            return []
        with self.lock:
            rows = self.db.execute(f"SELECT id FROM lessons WHERE norm IN ({','.join('?'*len(norms))})", norms).fetchall()
        return [r['id'] for r in rows]

    def record_lesson_uses(self, decision_id, lesson_ids):
        if not lesson_ids:
            return
        with self.lock:
            self.db.executemany('INSERT OR IGNORE INTO lesson_uses VALUES(?,?)', [(decision_id, i) for i in lesson_ids])
            self.db.execute(f"UPDATE lessons SET uses=uses+1 WHERE id IN ({','.join('?'*len(lesson_ids))})", lesson_ids)
            self.db.commit()

    def lesson_regrets(self, lesson_id):
        """Regrets of graded decisions that were shown the lesson, and of those decided after it existed without seeing it."""
        with self.lock:
            shown = self.db.execute('SELECT e.result_json FROM lesson_uses u JOIN evaluations e ON e.decision_id=u.decision_id WHERE u.lesson_id=?', (lesson_id,)).fetchall()
            hidden = self.db.execute('SELECT e.result_json FROM decisions d JOIN evaluations e ON e.decision_id=d.id JOIN lessons l ON l.id=? '
                                     'WHERE d.created_at>=l.created_at AND d.id NOT IN (SELECT decision_id FROM lesson_uses WHERE lesson_id=?)', (lesson_id, lesson_id)).fetchall()
        return ([json.loads(r['result_json']).get('regret') for r in shown], [json.loads(r['result_json']).get('regret') for r in hidden])

    def set_lesson_credit(self, lesson_id, credit):
        with self.lock:
            self.db.execute('UPDATE lessons SET regret_with=?, regret_without=? WHERE id=?', (credit.get('regret_with'), credit.get('regret_without'), lesson_id))
            self.db.commit()

    def retire_lesson(self, lesson_id, reason):
        with self.lock:
            self.db.execute('UPDATE lessons SET active=0, retired_reason=? WHERE id=?', (reason[:300], lesson_id))
            self.db.commit()

    def restore_lesson(self, lesson_id):
        with self.lock:
            self.db.execute('UPDATE lessons SET active=1, retired_reason=NULL WHERE id=?', (lesson_id,))
            self.db.commit()

    def save_case(self, decision_id, incident_id, signature):
        with self.lock:
            self.db.execute('INSERT OR REPLACE INTO cases VALUES(?,?,?)', (decision_id, incident_id, json.dumps(signature)))
            self.db.commit()

    def case_signature(self, decision_id):
        with self.lock:
            row = self.db.execute('SELECT signature_json FROM cases WHERE decision_id=?', (decision_id,)).fetchone()
        return json.loads(row['signature_json']) if row else None

    def cases(self, exclude_incident=None):
        """Graded decisions with a situation signature; the current incident is excluded so no case grades itself."""
        with self.lock:
            rows = self.db.execute(
                'SELECT d.id,d.incident_id,d.tick,d.event_type,d.decision_json,d.outcome_json,e.result_json,r.diagnosis_json,c.signature_json '
                'FROM cases c JOIN decisions d ON d.id=c.decision_id JOIN evaluations e ON e.decision_id=d.id '
                'LEFT JOIN reflections r ON r.decision_id=d.id WHERE d.status=? AND (? IS NULL OR d.incident_id!=?) ORDER BY d.id',
                ('applied', exclude_incident, exclude_incident)).fetchall()
        return [dict(r) for r in rows]

    def episodes(self):
        """One row per incident, oldest first, with what is needed for a learning curve."""
        with self.lock:
            incidents = self.db.execute('SELECT incident_id, MIN(created_at) AS started_at, MIN(id) AS first_id, COUNT(*) AS decisions FROM decisions GROUP BY incident_id ORDER BY started_at').fetchall()
            out = []
            for inc in incidents:
                rows = self.db.execute('SELECT d.id,d.outcome_json,e.result_json FROM decisions d LEFT JOIN evaluations e ON e.decision_id=d.id WHERE d.incident_id=? ORDER BY d.id', (inc['incident_id'],)).fetchall()
                evals = [json.loads(r['result_json']) for r in rows if r['result_json']]
                outcomes = [json.loads(r['outcome_json']) for r in rows if r['outcome_json']]
                sur = self.db.execute('SELECT s.distance,s.divergent FROM surprises s JOIN decisions d ON d.id=s.decision_id WHERE d.incident_id=?', (inc['incident_id'],)).fetchall()
                measured = [s['distance'] for s in sur if s['distance'] is not None]
                shown = self.db.execute('SELECT COUNT(DISTINCT u.lesson_id) AS n FROM lesson_uses u JOIN decisions d ON d.id=u.decision_id WHERE d.incident_id=?', (inc['incident_id'],)).fetchone()['n']
                before = self.db.execute('SELECT COUNT(*) AS n FROM cases c JOIN evaluations e ON e.decision_id=c.decision_id WHERE c.decision_id<?', (inc['first_id'],)).fetchone()['n']
                out.append(dict(incident_id=inc['incident_id'], started_at=inc['started_at'], decisions=inc['decisions'],
                                regrets=[e.get('regret') for e in evals],
                                judgement_gaps=sum(e.get('gap_type') == 'judgement' for e in evals), execution_gaps=sum(e.get('gap_type') == 'execution' for e in evals),
                                surprise_checks=len(sur), divergences=sum(s['divergent'] for s in sur),
                                mean_surprise=round(sum(measured)/len(measured), 4) if measured else None,
                                people_burnt=sum(o.get('people_burnt', 0) for o in outcomes), cases_before=before, lessons_shown=shown))
        return out

    def save_patch(self, decision_id, version_id, report_path):
        with self.lock:
            self.db.execute('INSERT INTO patches(decision_id,version_id,report_path,created_at) VALUES(?,?,?,?)',
                            (decision_id, version_id, str(report_path), time.time()))
            self.db.commit()

    def patches(self):
        with self.lock:
            rows = self.db.execute('SELECT * FROM patches ORDER BY id DESC').fetchall()
        return [dict(r) for r in rows]

    def save_forecast(self, decision_id, kind, result):
        """``kind`` is 'before' (world as seen, current orders) or 'after' (with the applied decision)."""
        with self.lock:
            self.db.execute('INSERT OR REPLACE INTO forecasts VALUES(?,?,?,?)', (decision_id, kind, result.get('issued_at'), json.dumps(result)))
            self.db.commit()

    def forecast(self, decision_id, kind='after'):
        with self.lock:
            row = self.db.execute('SELECT forecast_json FROM forecasts WHERE decision_id=? AND kind=?', (decision_id, kind)).fetchone()
        return json.loads(row['forecast_json']) if row else None

    def save_surprise(self, decision_id, result):
        with self.lock:
            self.db.execute('INSERT INTO surprises(decision_id,tick,distance,threshold,divergent,surprise_json) VALUES(?,?,?,?,?,?)',
                            (decision_id, result['tick'], result['distance'], result['threshold'], int(result['divergent']), json.dumps(result)))
            self.db.commit()

    def surprises(self, incident_id):
        with self.lock:
            rows = self.db.execute('SELECT s.decision_id,s.tick,s.distance,s.threshold,s.divergent,s.surprise_json FROM surprises s '
                                   'JOIN decisions d ON d.id=s.decision_id WHERE d.incident_id=? ORDER BY s.id', (incident_id,)).fetchall()
        return [dict(r, surprise=json.loads(r['surprise_json'])) for r in rows]
