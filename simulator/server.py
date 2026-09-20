"""Run with python3 -m simulator.server; open http://127.0.0.1:8765."""
import argparse
import atexit
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import time
import zlib
from urllib.parse import urlparse

from .analysis import Analyst
from .blackbox import BlackBox
from .engine import Simulation
from .geo import PLACE
from .happyrobot import HappyRobot, EDITOR
from . import experience, worlds

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / '.runtime'


def _env_file():
    values = {}
    path = ROOT / '.env'
    if not path.exists():
        return values
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def local_host(host_header):
    """Only the loopback interface may read local API keys."""
    return (host_header or '').split(':')[0] in {'127.0.0.1', 'localhost'}


class Controller:
    def __init__(self):
        self.lock = threading.RLock()
        self.sim = Simulation(drone_count=2)
        self.robot = HappyRobot()
        self.busy = False
        self.pending_fires = []
        self.reset_pending = False
        self.auto = False
        self.running = False
        self.speed = 2
        self.error = None
        self.run_evidence = ''
        self.recording = False
        self.recorded_frames = []
        self.calls = 0
        self.latency = None
        self.stop = threading.Event()
        self.frames = [self.snapshot()]
        self.cursor = None
        self.next_decision = 0
        self.repair_attempts = 0
        # Flight recorder + post-mortem pipeline. Lessons persist across incidents.
        self.box = BlackBox(RUNTIME/'blackbox.sqlite')
        self.analyst = Analyst(self.box, self.robot, self._set_lessons, self._log)
        self.last_decision_id = None
        self.sim.lessons = self.box.active_lessons(5)
        # Possible worlds: the forecast for the plan in force, and how far observation has drifted from it.
        self.forecast = None
        self.forecast_consumed = False
        self.forecast_thread = None
        self.forecast_before = None
        self.surprises = []
        # Experience replay: the situation signature and retrieved cases/lessons shown to the last decision.
        self.experience = None
        self.experience_before = None
        threading.Thread(target=self._clock, daemon=True).start()

    def _set_lessons(self, lessons):
        with self.lock:
            self.sim.lessons = list(lessons)

    def _log(self, source, message, **extra):
        with self.lock:
            self.sim.log(source, message, **extra)

    def _record(self, payload, decision, status, reason=''):
        """Freeze the pre-apply world and the decision in the black box; returns the record id."""
        if self.last_decision_id is not None:
            self.box.finish_outcome(self.last_decision_id, self.sim)
        run_id = getattr(self.robot, 'last_run_id', None)
        self.last_decision_id = self.box.record_decision(self.sim, payload, run_id, self.latency, decision, status, reason)
        return self.last_decision_id

    def _analyse(self, decision_id, payload):
        """Schedule the post-mortem pipeline; it never blocks the clock."""
        run_id = getattr(self.robot, 'last_run_id', None)
        threading.Thread(target=self.analyst.analyse, args=(decision_id, payload, run_id, getattr(self.robot, 'last_listing', '')), daemon=True).start()

    def _finish_outcome(self):
        if self.last_decision_id is not None:
            self.box.finish_outcome(self.last_decision_id, self.sim)
            self.last_decision_id = None

    def _forecast_after(self, decision_id, world):
        """Ensemble for the plan just applied, computed off the clock; becomes the divergence baseline."""
        def run():
            try:
                result = worlds.forecast(world)
                self.box.save_forecast(decision_id, 'after', result)
                with self.lock:
                    if self.last_decision_id != decision_id or self.sim.incident_id != world.incident_id:
                        return
                    self.forecast = result
                    self.forecast_consumed = False
                    plan = result['plans'].get('current_orders', {})
                    threatened = [k for k, v in (plan.get('districts') or {}).items() if v['p_fire_within_8'] >= .5]
                    self.sim.log('forecast', f"Possible worlds t+{result['horizon']}: {plan.get('branches', 0)} branches, dispersion {plan.get('dispersion')}, "
                                 f"~{plan.get('expected_burning_cells')} burning cells expected"+(f"; fire likely within {worlds.THREAT_CELLS} cells of {', '.join(threatened)}" if threatened else ''))
            except Exception as exc:
                self._log('forecast', f'Forecast failed: {str(exc)[:200]}')
        thread = threading.Thread(target=run, daemon=True)
        self.forecast_thread = thread
        thread.start()

    def _check_forecast(self):
        """At each forecast checkpoint, compare what is observed with what was forecast; raise forecast_divergence once."""
        forecast = self.forecast
        if not forecast or self.forecast_consumed or self.sim.phase != 'active':
            return
        elapsed = self.sim.tick-forecast['issued_at']
        if elapsed <= 0 or elapsed > forecast['horizon'] or elapsed % worlds.CHECKPOINT_EVERY:
            return
        result = worlds.surprise(forecast, self.sim)
        if result is None:
            return
        self.surprises.append(result)
        self.surprises = self.surprises[-40:]
        if self.last_decision_id is not None:
            self.box.save_surprise(self.last_decision_id, result)
        if not result['divergent']:
            return
        self.forecast_consumed = True
        self.sim.divergence = result
        self.sim.log('forecast', f"Forecast divergence: observed world is {result['distance']} from the forecast (threshold {result['threshold']}). "
                     +('; '.join(result['what_changed'][:3]) or 'no single named cause'))
        if self.sim.called and not self.sim.pending_decision_event:
            self.sim.pending_decision_event = 'forecast_divergence'

    def postmortem(self):
        with self.lock:
            incident = self.sim.incident_id
        rows = self.box.list_decisions(incident)
        for row in rows:
            for key in ('decision_json', 'outcome_json', 'signals_json', 'result_json', 'diagnosis_json'):
                raw = row.pop(key, None)
                row[key[:-5]] = json.loads(raw) if raw else None
        forecasts = {row['id']: self.box.forecast(row['id']) for row in rows}
        for row in rows:
            plan = ((forecasts.get(row['id']) or {}).get('plans') or {}).get('current_orders') or {}
            row['forecast'] = dict(dispersion=plan.get('dispersion'), expected_burning_cells=plan.get('expected_burning_cells'),
                                   threatened=[k for k, v in (plan.get('districts') or {}).items() if v['p_fire_within_8'] >= .5]) if plan else None
        return dict(incident_id=incident, decisions=rows, lessons=self.box.active_lessons(5), patches=self.box.patches(),
                    surprises=self.box.surprises(incident))

    def snapshot(self):
        return zlib.compress(json.dumps(self.sim.state()).encode())

    def record(self):
        frame=self.snapshot()
        self.frames.append(frame)
        if self.recording:
            self.recorded_frames.append(frame)
            if len(self.recorded_frames)>=1500:self.recording=False
        if len(self.frames)>1500:
            self.frames.pop(0)

    def _clock(self):
        while not self.stop.wait(1/self.speed):
            with self.lock:
                if not self.running or self.busy or self.cursor is not None:
                    continue
                self.sim.step()
                self._check_forecast()
                if self.sim.phase != 'active':
                    self.auto = False
                if self.sim.phase == 'finished':
                    self.running = False
                    self._finish_outcome()
                self.record()
                if self.sim.phase == 'finished':
                    self.recording = False
                if self.auto and self.sim.called and (self.sim.pending_decision_event or self.sim.tick>=self.next_decision):
                    self.request_decision(self.sim.pending_decision_event or 'local_observation')

    def state(self):
        with self.lock:
            frame = self.sim.state() if self.cursor is None else json.loads(zlib.decompress(self.frames[self.cursor]))
            return dict(frame, pending_fires=len(self.pending_fires), reset_pending=self.reset_pending, busy=self.busy, auto=self.auto,running=self.running,speed=self.speed,
                        frame_index=len(self.frames)-1 if self.cursor is None else self.cursor,
                        recording=self.recording,recorded_frames=len(self.recorded_frames),
                        frame_count=len(self.frames),replay=self.cursor is not None,live_tick=self.sim.tick,
                        connected=self.robot.connected, error=self.error, workflow_url=EDITOR,
                        workflow_calls=self.calls, latency=self.latency, run_evidence=self.run_evidence,
                        forecast=self._forecast_view(), divergence=self.sim.divergence, surprises=self.surprises[-12:],
                        experience=self.experience)

    def _experience_context(self, world, event_type):
        """Similar graded past decisions and the lessons relevant to this situation, computed off the lock."""
        sig = experience.signature(world, event_type)
        return dict(signature=sig, cases=experience.retrieve(self.box, sig, exclude_incident=world.incident_id),
                    lessons=experience.relevant_lessons(self.box, sig))

    def learning(self):
        with self.lock:
            shown = self.experience
        return dict(episodes=experience.learning_curve(self.box), lessons=self.box.lesson_rows(active_only=False), experience=shown)

    def _forecast_view(self):
        if not self.forecast:
            return None
        plan = self.forecast['plans'].get('current_orders') or {}
        return dict(issued_at=self.forecast['issued_at'], horizon=self.forecast['horizon'], branches=plan.get('branches'), dispersion=plan.get('dispersion'),
                    expected_burning_cells=plan.get('expected_burning_cells'), districts=plan.get('districts'), burn_probability=plan.get('burn_probability', []),
                    believed_burning_cells=self.forecast.get('believed_burning_cells'), consumed=self.forecast_consumed, valid=plan.get('valid', True))

    def request_decision(self, event='local_observation'):
        if self.sim.phase != 'active':
            raise ValueError('Fire is out; vehicles are returning or at station.')
        if self.busy:
            raise ValueError('A decision is already running.')
        if not self.sim.called:
            raise ValueError('Send the farmer report first.')
        if self.cursor is not None:
            raise ValueError('Return to Live before requesting decisions.')
        if event != 'command_rejected':
            self.repair_attempts = 0
        payload = self.sim.payload(event)
        self.sim.pending_decision_event = None
        self.busy = True
        self.error = None
        # A corrective retry sees the same frozen world, so it reuses the forecast already computed for it.
        world = None if event == 'command_rejected' else copy.deepcopy(self.sim)
        threading.Thread(target=self._decide, args=(payload,self.sim.tick,world), daemon=True).start()

    def _decide(self, payload, tick, world=None):
        start = time.monotonic()
        retry = False
        corrective = payload.get('event_type') == 'command_rejected'
        before = self.forecast_before if corrective else None
        context = self.experience_before if corrective else None
        try:
            # What the believed world does under current orders, so the agent plans against futures, not a snapshot.
            if world is not None:
                try:
                    before = worlds.forecast(world)
                except Exception as exc:
                    self._log('forecast', f'Pre-decision forecast skipped: {str(exc)[:200]}')
                try:
                    context = self._experience_context(world, payload.get('event_type'))
                except Exception as exc:
                    self._log('experience', f'Case retrieval skipped: {str(exc)[:200]}')
            if before or context:
                state = json.loads(payload['world_state'])
                if before:
                    state['possible_worlds'] = worlds.agent_view(before)
                if context:
                    state['similar_cases'] = context['cases']
                    state['lessons_learned'] = [l['rule'] for l in context['lessons']]
                payload['world_state'] = json.dumps(state)
            if context and context['cases']:
                self._log('experience', f"{len(context['cases'])} similar past decision(s) retrieved (closest {context['cases'][0]['similarity_distance']}, regret {context['cases'][0]['regret']}); "
                                        f"{len(context['lessons'])} lesson(s) relevant")
            decision, evidence = self.robot.decide(payload)
            with self.lock:
                if self.reset_pending:return
                self.calls += 1
                self.latency = round(time.monotonic()-start, 1)
                self.run_evidence = evidence
                decision_id = self._record(payload, decision, 'pending')
                if before:
                    self.box.save_forecast(decision_id, 'before', before)
                if context:
                    self.box.save_case(decision_id, payload['incident_id'], context['signature'])
                    self.box.record_lesson_uses(decision_id, [l['id'] for l in context['lessons']])
                    self.experience = dict(context, decision_id=decision_id)
                self.sim.divergence = None
                try:
                    self.sim.apply(decision,payload['event_id'],payload['incident_id'],tick)
                except ValueError as exc:
                    self.box.set_status(decision_id, 'rejected', str(exc)[:500])
                    self._analyse(decision_id, payload)
                    raise
                self.box.set_status(decision_id, 'applied')
                self._analyse(decision_id, payload)
                self.forecast = None
                self._forecast_after(decision_id, copy.deepcopy(self.sim))
                self.next_decision = self.sim.tick + 16
                self.record()
        except Exception as exc:
            with self.lock:
                if self.reset_pending:return
                message = str(exc)[:800]
                if not isinstance(exc, ValueError):
                    # The run itself failed: keep the frozen state so the oracle can still grade it.
                    self.latency = round(time.monotonic()-start, 1)
                    self.box.record_decision(self.sim, payload, getattr(self.robot,'last_run_id',None), self.latency, {}, 'error', message)
                if isinstance(exc, ValueError) and self.repair_attempts < 1 and self.cursor is None:
                    self.repair_attempts += 1
                    retry = True
                    self.sim.last_result = dict(status='rejected',reason=message,
                        instruction='Choose a new valid command using CURRENT observations. For contain use an exact x,y pair from drone_telemetry.safe_containment_positions, not a burning cell.')
                    self.sim.log('system','Command rejected; requesting one corrected HappyRobot decision. '+message)
                else:
                    self.error = message
                    self.running = self.auto = False
                    self.sim.log('system',self.error)
                self.record()
        finally:
            with self.lock:
                self.busy = False
                self.forecast_before = before if retry else None
                self.experience_before = context if retry else None
                if self.reset_pending:
                    self.action('reset',{})
                else:
                    for x,y in self.pending_fires:
                        self.sim.add_fire(x,y)
                    if self.pending_fires:self.record()
                    self.pending_fires.clear()
                    if retry:self.request_decision('command_rejected')

    def action(self, action, data):
        with self.lock:
            if action == 'stop_recording':
                self.recording=False
                self.running=False
                return
            if action == 'pause':
                self.running = False
                return
            if action == 'seek':
                index = int(data.get('index',0))
                if not 0<=index<len(self.frames):
                    raise ValueError('Frame outside replay.')
                self.running = False
                self.cursor = index
                return
            if action == 'live':
                self.cursor = None
                return
            if self.cursor is not None and action != 'reset':
                raise ValueError('Return to Live to change the simulation. Replay never reruns AI.')
            if self.busy and action == 'reset':
                self.reset_pending=True
                self.running=self.auto=False
                return
            if self.busy and action == 'add_fire' and not self.reset_pending:
                x,y=data.get('x'),data.get('y')
                self.sim.validate_ignition(x,y)
                if (x,y) not in self.pending_fires:self.pending_fires.append((x,y))
                return
            if self.busy:
                raise ValueError('HappyRobot is deciding. You can pause or inspect the timeline.')
            if action == 'reset':
                self.reset_pending=False
                self.pending_fires.clear()
                self.repair_attempts=0
                self.cursor = None
                self._finish_outcome()
                self.sim = Simulation(fleet_counts=self.sim.fleet_counts())
                self.sim.lessons = self.box.active_lessons(5)
                self.recording=False
                self.error = None
                self.auto = self.running = False
                self.calls = 0
                self.run_evidence = ''
                self.latency = None
                self.frames = [self.snapshot()]
                self.next_decision = 0
                self.forecast = None
                self.forecast_consumed = False
                self.forecast_before = None
                self.surprises = []
                self.experience = None
                self.experience_before = None
            elif action == 'lesson':
                lesson_id, active = int(data.get('id')), bool(data.get('active', True))
                if active:
                    self.box.restore_lesson(lesson_id)
                else:
                    self.box.retire_lesson(lesson_id, 'retired by operator')
                self.sim.lessons = self.box.active_lessons(5)
                self.sim.log('operator', f"Lesson {lesson_id} {'restored' if active else 'retired'}.")
            elif action == 'fleet':
                self.sim.configure_fleet(data.get('count'),**data.get('counts',{}))
                self.sim.observe()
            elif action == 'add_fire':
                self.sim.add_fire(data.get('x'),data.get('y'))
            elif action == 'place_fire':
                self.sim.place_fire(data.get('x'),data.get('y'))
            elif action == 'record_run':
                if 'x' in data or 'y' in data:self.sim.set_wind(x=data.get('x'),y=data.get('y'))
                self.recorded_frames=[self.snapshot()]
                self.recording=True
                self.sim.ignite()
                if not self.sim.called:self.sim.farmer_call()
                self.running=self.auto=True
                self.request_decision('farmer_call' if self.calls==0 else 'local_observation')
            elif action == 'ignite':
                self.sim.ignite()
            elif action == 'step':
                self.sim.step()
            elif action == 'spread_factor':
                self.sim.set_spread_factor(data.get('value'))
                if self.sim.called:
                    self.request_decision('forecast_update')
            elif action == 'wind':
                self.sim.set_wind(data.get('direction','east'),data.get('x'),data.get('y'))
                if self.sim.called:
                    self.request_decision('forecast_update')
            elif action == 'call':
                self.sim.farmer_call(str(data.get('message',''))[:2000].strip())
                self.auto = self.running = True
                self.request_decision('farmer_call')
            elif action in {'decision','auto'}:
                self.auto = action=='auto'
                self.running = self.auto
                self.request_decision()
            elif action == 'play':
                self.running = True
            elif action == 'speed':
                speed = int(data.get('speed',2))
                if speed not in {1,2,4,8}:
                    raise ValueError('Invalid playback speed.')
                self.speed = speed
            else:
                raise ValueError('Unknown action.')
            self.record()


def serve(port=8765):
    controller = Controller()
    atexit.register(controller.robot.close)
    static = Path(__file__).parent/'static'

    class Handler(BaseHTTPRequestHandler):
        def reply(self, code, body, content_type='application/json'):
            if not isinstance(body, bytes):
                body = json.dumps(body).encode()
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urlparse(self.path).path
            if path == '/api/recording':
                with controller.lock:
                    frames=[json.loads(zlib.decompress(f)) for f in controller.recorded_frames]
                self.reply(200,dict(format='los-panaderos-recording-v1',frames=frames))
            elif path == '/api/state':
                self.reply(200, controller.state())
            elif path == '/api/learning':
                self.reply(200, controller.learning())
            elif path == '/api/postmortem':
                self.reply(200, controller.postmortem())
            elif path == '/api/config':
                if not local_host(self.headers.get('Host')):
                    self.reply(403, {'error': 'Local config only.'})
                    return
                env = _env_file()
                self.reply(200, dict(
                    cesium_token=env.get('CESIUM_API_KEY') or None,
                    nasa_key=env.get('NASA_KEY') or None,
                    place=dict(PLACE)))
            elif path in {'/', '/app.js', '/ops.js', '/observation-map.js', '/vendor/bootstrap-icons.js', '/style.css', '/flamethrower-cursor.svg', '/maps/brunete.jpg', '/maps/brunete-illustrated.png'}:
                name = 'index.html' if path == '/' else path[1:]
                types = {'.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.css': 'text/css', '.jpg':'image/jpeg', '.png':'image/png', '.svg':'image/svg+xml'}
                file = static/name
                self.reply(200, file.read_bytes(), types[file.suffix])
            else:
                self.reply(404, {'error': 'Not found'})

        def do_POST(self):
            # Reject cross-origin requests to the local authenticated MCP bridge.
            allowed = {f'http://127.0.0.1:{port}', f'http://localhost:{port}'}
            if self.headers.get('Origin') not in allowed or self.headers.get('X-Simulator-Request') != '1':
                self.reply(403, {'error': 'Use the local simulator interface.'})
                return
            if self.path != '/api/action':
                self.reply(404, {'error': 'Not found'})
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 8192:
                    raise ValueError('Invalid request size.')
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError('Expected JSON object.')
                controller.action(data.get('action'), data)
                self.reply(200, controller.state())
            except (ValueError, TypeError) as exc:
                self.reply(400, {'error': str(exc)})

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    print(f'Los Panaderos: http://127.0.0.1:{port}', flush=True)
    print('HappyRobot development workflow; farmer call is a simulated transcript.', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        controller.stop.set()
        controller.robot.close()
        controller.box.close()
        server.server_close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8765)
    serve(parser.parse_args().port)
