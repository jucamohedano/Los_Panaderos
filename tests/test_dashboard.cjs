const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.join(__dirname, '..');
const source = fs.readFileSync(path.join(root, 'simulator/static/app.js'), 'utf8');
const chrome = source.slice(source.indexOf('let spreadDirty=false;'));
const html = fs.readFileSync(path.join(root, 'simulator/static/index.html'), 'utf8');
const copy = value => JSON.parse(JSON.stringify(value));
const response = (body, status = 200) => ({ok: status < 400, status, json: async () => copy(body)});
const deferred = () => {let resolve, reject; const promise = new Promise((yes, no) => {resolve = yes; reject = no;}); return {promise, resolve, reject};};
const flush = async () => {for (let i = 0; i < 8; i++) await new Promise(resolve => setImmediate(resolve));};

function frame(overrides = {}) {
  const drone = {x: 12, y: 32, status: 'at_station', mode: 'hold', drone_id: 'drone-1', role: 'extinguisher', observed_fire: []};
  const truck = {x: 12, y: 32, status: 'at_station', truck_id: 'engine-1', role: 'truck', observed_fire: []};
  return {...{width: 80, height: 56, tick: 0, incident_id: 'test-incident', mission: 'Awaiting a report', wind: [1, 0],
    cells: Array.from({length: 56}, () => Array.from({length: 80}, () => ({heat: 0, fuel: 1}))),
    base: [12, 32], town: [10, 35], farm: [73, 21], drone, truck, scouts: [], extinguishers: [drone], trucks: [truck],
    fleet_counts: {scouts: 0, extinguishers: 1, trucks: 1}, people: {farm: {x: 73, y: 21, count: 100, burnt: 0, status: 'unwarned', kind: 'farm', name: 'El Álamo Farm', refuge: [60, 12]}},
    history: [], observation: [], observed_cells: [], roads: [], geography: null, satellite: null, rules: {spread_factor: 0.5},
    burning: 0, extinguished: 0, crew_extinguished: 0, ignited: false, called: false, phase: 'active', busy: false,
    running: false, replay: false, reset_pending: false, pending_fires: 0, speed: 2, frame_count: 2, frame_index: 0,
    recording: false, recorded_frames: 0, workflow_calls: 0, workflow_url: '/workflow', run_evidence: '', error: null}, ...overrides};
}
const recording = frames => ({format: 'los-panaderos-recording-v1', frames});

class Element {
  constructor(id = '') {this.id = id; this.dataset = {}; this.hidden = false; this.disabled = false; this.value = '0'; this.open = false; this.children = []; this.attributes = {}; this.style = {}; this._text = ''; const classes = new Set(); this.classList = {toggle: (key, enabled) => enabled ? classes.add(key) : classes.delete(key), contains: key => classes.has(key), add: key => classes.add(key)};}
  set textContent(value) {if (value === this.failOnText) throw Error('Injected render failure'); this._text = String(value);}
  get textContent() {return this._text;}
  setAttribute(key, value) {this.attributes[key] = String(value);}
  getAttribute(key) {return this.attributes[key];}
  replaceChildren(...children) {this.children = children;}
  append(...children) {this.children.push(...children);}
  click() {if (!this.disabled) return this.onclick?.({currentTarget: this, target: this});}
  showModal() {this.open = true;}
  getBoundingClientRect() {return {left: 0, top: 0, width: 800, height: 560};}
}

test('adaptive summary precedes maps and separates sent experience from confirmed use', async () => {
  const h = await harness();
  assert.ok(html.indexOf('id="adaptStrip"') < html.indexOf('<main class="maps">'));
  h.put('s', frame({forecast: {branches: 8, districts: {farm: {p_fire_within_8: .75}}},
    experience: {cases: [{decision_id: 2}], lessons: [{id: 1}]}}));
  h.run('render(s)');
  assert.match(h.elements.get('adFutureMain').textContent, /8/);
  assert.match(h.elements.get('adFutureSub').textContent, /75%/);
  assert.match(h.elements.get('adMemoryMain').textContent, /1 caso enviado/);
  assert.match(h.elements.get('adMemorySub').textContent, /uso no confirmado/);
  assert.match(h.elements.get('adChangeMain').textContent, /Sin comprobar/);
  h.run("lang='en'; render(s)");
  assert.match(h.elements.get('adMemorySub').textContent, /use unconfirmed/);
});

test('adaptation distinguishes divergence, checked state, reset and replay', async () => {
  const h = await harness();
  h.put('s', frame({divergence: {what_changed: ['<wind changed>']}, experience: {cases: [{}], lessons: []}}));
  h.run('render(s)');
  assert.ok(h.elements.get('adaptChange').classList.contains('is-diverged'));
  assert.equal(h.elements.get('adChangeSub').textContent, '<wind changed>');
  h.put('s', frame({surprises: [{tick: 4, divergent: false}]})); h.run('render(s)');
  assert.match(h.elements.get('adChangeMain').textContent, /Dentro de lo previsto/);
  assert.equal(h.elements.get('adaptChange').classList.contains('is-diverged'), false);
  h.put('s', frame()); h.run('render(s)');
  assert.equal(h.elements.get('adMemoryMain').textContent, '—');
  h.put('s', frame({replay: true, experience: {cases: [{}]}, divergence: {what_changed: ['stale']}})); h.run('render(s)');
  assert.equal(h.elements.get('adMemoryMain').textContent, '—');
  assert.match(h.elements.get('adChangeSub').textContent, /Reproducción/);
});

test('adaptive evidence opens in a dialog without page scrolling and hides live panels in replay', async () => {
  const h = await harness();
  h.elements.get('adaptForecast').click();
  assert.equal(h.elements.get('insightDialog').open, true);
  assert.equal(h.elements.get('forecastPanel').hidden, false);
  assert.equal(h.elements.get('learningPanel').hidden, true);
  h.put('s', frame({replay: true})); h.run('render(s)');
  assert.equal(h.elements.get('forecastPanel').hidden, true);
  assert.equal(h.elements.get('insightReplay').hidden, false);
});

test('a completed replan never relabels its divergent checkpoint as a passed check', async () => {
  const h = await harness();
  h.put('s', frame({forecast: {issued_at: 24, branches: 8},
    surprises: [{tick: 24, divergent: true}], divergence: null}));
  h.run('render(s)');
  assert.equal(h.elements.get('adChangeMain').textContent, 'Nuevo plan sin comprobar');
  assert.match(h.elements.get('adChangeSub').textContent, /anterior se desvió en t=24/);
  h.run('s.surprises.push({tick:28,divergent:false}); render(s)');
  assert.equal(h.elements.get('adChangeMain').textContent, 'Dentro de lo previsto');
  h.run('s.forecast.issued_at=28; render(s)');
  assert.equal(h.elements.get('adChangeMain').textContent, 'Nuevo plan sin comprobar');
});

test('regret summary is descriptive, refreshes with panels closed and does not retain failed data', async () => {
  const h = await harness();
  h.handler = () => response({episodes: [{graded: 1, mean_regret: 100}, {graded: 1, mean_regret: 20}], lessons: []});
  await h.run('refreshLearning()');
  assert.equal(h.elements.get('learningPanel').open, false);
  assert.equal(h.elements.get('adLearnMain').textContent, '100 → 20');
  assert.match(h.elements.get('adaptLearn').title, /no demuestra aprendizaje/);
  assert.match(h.elements.get('adLearnChart').innerHTML, /polyline/);
  h.handler = () => {throw Error('offline');};
  await h.run('renderLearning()');
  assert.equal(h.elements.get('adLearnMain').textContent, '—');
  assert.match(h.elements.get('adLearnSub').textContent, /no disponible/);
});

test('late learning responses cannot overwrite a newer response or replay', async () => {
  const h = await harness(), gate = deferred();
  h.handler = () => gate.promise;
  const slow = h.run('renderLearning()');
  h.handler = () => response({episodes: [{graded: 1, mean_regret: 10}]});
  await h.run('renderLearning()');
  gate.resolve(response({episodes: [{graded: 1, mean_regret: 999}]})); await slow;
  assert.equal(h.elements.get('adLearnMain').textContent, '10 → 10');
  const replayGate = deferred(); h.handler = () => replayGate.promise;
  const late = h.run('renderLearning()');
  h.put('s', frame({replay: true})); h.run('render(s)');
  replayGate.resolve(response({episodes: [{graded: 1, mean_regret: 0}]})); await late;
  assert.equal(h.elements.get('adLearnMain').textContent, '—');
});

async function harness() {
  const elements = new Map([...html.matchAll(/\bid="([^"]+)"/g)].map(match => [match[1], new Element(match[1])]));
  elements.get('error').hidden = true;
  const live = new Element('live'); live.dataset.action = 'live';
  const actions = [live];
  for (const match of html.matchAll(/<button\b[^>]*data-action="([^"]+)"[^>]*>/g)) {
    const id = match[0].match(/\bid="([^"]+)"/)?.[1];
    if (match[1] === 'live') continue;
    const element = id ? elements.get(id) : new Element(match[1]);
    element.dataset.action = match[1]; actions.push(element);
  }
  const toolbar = [...['play', 'btnAsk', 'resetSim', 'addFire', 'recordRun', 'stopRecord', 'playRecord', 'downloadRecord', 'btnIgnite', 'btnCall', 'speed', 'applyFleet', 'fleet-trucks', 'fleet-scouts', 'fleet-extinguishers'].map(id => elements.get(id)), ...actions];
  const intervals = new Map(), timeouts = new Map(); let nextTimer = 0;
  const h = {elements, live, intervals, timeouts, requests: [], server: frame(), handler: null};
  const sandbox = {console, URL, Blob, Date, Set, Map, Number, JSON, Math, Error, performance: {now: () => 0},
    document: {getElementById: id => elements.get(id), createElement: () => new Element(), body: new Element(), documentElement: {},
      querySelectorAll: selector => selector === '[data-action]' ? actions : selector.startsWith('.toolbar') ? toolbar : [],
      querySelector: selector => selector === '[data-action="live"]' ? live : null},
    setTimeout: (fn, ms) => {const id = ++nextTimer; timeouts.set(id, {fn, ms}); return id;}, clearTimeout: id => timeouts.delete(id),
    setInterval: (fn, ms) => {const id = ++nextTimer; intervals.set(id, {fn, ms}); return id;}, clearInterval: id => intervals.delete(id),
    fetch: async (url, options = {}) => {const request = {url, options, body: options.body ? JSON.parse(options.body) : null}; h.requests.push(request); if (h.handler) return h.handler(request); if (url === '/api/state' || url === '/api/action') return response(h.server); if (url === '/api/recording') return response(recording([])); throw Error('Unexpected request: ' + url);},
    addEventListener: () => {}, requestAnimationFrame: () => {}, AerialView: {draw: () => {}}};
  sandbox.window = sandbox;
  h.context = vm.createContext(sandbox);
  h.run = code => vm.runInContext(code, h.context);
  h.value = code => copy(h.run(code));
  h.put = (name, value) => {h.context[name] = value;};
  h.import = async frames => {const text = JSON.stringify(recording(frames)); await elements.get('openRecording').onchange({target: {files: [{size: Buffer.byteLength(text), text: async () => text}], value: 'recording.json'}}); await flush();};
  vm.runInContext(chrome, h.context);
  await flush();
  return h;
}

test('late live poll cannot overwrite local replay', async () => {
  const h = await harness(), gate = deferred();
  h.handler = () => gate.promise;
  const poll = h.run('poll()');
  h.put('frames', [frame({tick: 5, mission: 'Recorded'})]);
  h.run('recordingPlayback=frames; showRecorded(0)');
  gate.resolve(response(frame({tick: 77, running: true})));
  await poll;
  assert.equal(h.run('state.tick'), 5);
  assert.equal(h.run('state.replay'), true);
});

test('failed pause prevents recording import and keeps the failure visible', async () => {
  const h = await harness(); h.server.running = true;
  h.handler = request => request.url === '/api/action' ? response({error: 'Pause failed'}, 500) : response(h.server);
  await h.import([frame({tick: 5})]);
  assert.equal(h.run('recordingPlayback===null'), true);
  assert.equal(h.elements.get('error').hidden, false);
  await h.run('poll()');
  assert.equal(h.elements.get('error').hidden, false);
});

test('malformed recording is rejected before pause and does not stop polling', async () => {
  const h = await harness();
  await h.import([frame({wind: null})]);
  assert.equal(h.requests.filter(request => request.body?.action === 'pause').length, 0);
  assert.equal(h.run('recordingPlayback===null'), true);
  assert.equal(h.elements.get('error').hidden, false);
  const before = h.requests.length; await h.run('poll()');
  assert.equal(h.requests.length, before + 1);
});

test('client errors survive polling and clear on a successful action', async () => {
  const h = await harness();
  h.handler = request => request.body?.action === 'fleet' ? response({error: 'Bad fleet'}, 400) : response(h.server);
  assert.equal(await h.run("act('fleet',{counts:{trucks:0,scouts:0,extinguishers:0}})"), false);
  await h.run('poll()');
  assert.equal(h.elements.get('error').hidden, false);
  assert.match(h.elements.get('error').textContent, /Bad fleet/);
  await h.run("act('pause')");
  assert.equal(h.elements.get('error').hidden, true);
});

test('failed Live retains the current recording', async () => {
  const h = await harness();
  h.put('frames', [frame({tick: 8})]); h.run('recordingPlayback=frames;showRecorded(0)');
  h.handler = () => response({error: 'Live failed'}, 500);
  assert.equal(await h.run("act('live')"), false);
  assert.equal(h.run('recordingPlayback.length'), 1);
  assert.equal(h.run('state.tick'), 8);
});

test('double replay click cancels a pending start without leaking intervals', async () => {
  const h = await harness(), gate = deferred();
  h.handler = () => gate.promise;
  const first = h.elements.get('replayPlay').onclick();
  const second = h.elements.get('replayPlay').onclick();
  gate.resolve(response(frame({replay: true})));
  await Promise.all([first, second]);
  assert.equal(h.intervals.size, 0);
});

test('stale action responses cannot replace a newer accepted view', async () => {
  const h = await harness(), gate = deferred();
  h.handler = request => request.body?.action === 'pause' ? gate.promise : response(frame({incident_id: 'reset-incident'}));
  const old = h.run("act('pause')");
  await h.run("act('reset')");
  gate.resolve(response(frame({tick: 99})));
  await old;
  assert.equal(h.run('state.incident_id'), 'reset-incident');
  assert.equal(h.run('state.tick'), 0);
});

test('poll HTTP errors are visible and recovery does not erase action errors', async () => {
  const h = await harness();
  h.handler = request => response({error: request.url === '/api/state' ? 'Poll failed' : 'Action failed'}, 500);
  await h.run("act('fleet')"); await h.run('poll()');
  assert.equal(h.elements.get('connection').textContent, h.run('I18N[lang].serverUnavailable'));
  h.handler = () => response(h.server); await h.run('poll()');
  assert.notEqual(h.elements.get('connection').textContent, h.run('I18N[lang].serverUnavailable'));
  assert.match(h.elements.get('error').textContent, /Action failed/);
});

test('recording download failure is handled without pausing', async () => {
  const h = await harness();
  h.handler = request => request.url === '/api/recording' ? response({error: 'No recording'}, 500) : response(h.server);
  await assert.doesNotReject(async () => h.elements.get('playRecord').onclick());
  assert.equal(h.requests.filter(request => request.body?.action === 'pause').length, 0);
  assert.equal(h.elements.get('error').hidden, false);
});

test('unexpected recording-render failure rolls back playback and displayed state', async () => {
  const h = await harness(); h.elements.get('mission').failOnText = 'Broken render';
  await h.import([frame({tick: 9, mission: 'Broken render'})]);
  assert.equal(h.run('recordingPlayback===null'), true);
  assert.equal(h.run('state.tick'), 0);
  assert.equal(h.elements.get('error').hidden, false);
});

test('slow file read cannot take over after a newer Live request', async () => {
  const h = await harness(), gate = deferred();
  const loading = h.elements.get('openRecording').onchange({target: {files: [{size: 10, text: () => gate.promise}], value: 'slow.json'}});
  await h.run("act('live')");
  gate.resolve(JSON.stringify(recording([frame({tick: 9})])));
  await loading;
  assert.equal(h.run('recordingPlayback===null'), true);
  assert.equal(h.requests.filter(request => request.body?.action === 'pause').length, 0);
});

test('successful local recording supports zero extinguisher roles and local seeking', async () => {
  const h = await harness();
  const onlyTrucks = frame({drone: null, extinguishers: [], fleet_counts: {trucks: 1, scouts: 0, extinguishers: 0}});
  await h.import([onlyTrucks, {...copy(onlyTrucks), tick: 1}]);
  assert.equal(h.run('state.replay'), true);
  assert.equal(h.intervals.size, 1);
  const count = h.requests.length; await h.run("act('seek',{index:1})");
  assert.equal(h.requests.length, count);
  assert.equal(h.run('state.tick'), 1);
  h.run('stopReplay()');
});

test('recording validation covers data consumed by the renderer and preserves legacy data', async () => {
  const h = await harness();
  for (const mutate of [f => {f.wind = null;}, f => {f.cells[0][0] = null;}, f => {f.history = [null];}, f => {f.drone.route = 'bad';}, f => {f.geography = {observation_zones: [{polygon: null}]};}, f => {f.people.farm.count = -1;}, f => {f.satellite = {captured_at: 0, blocks: null};}]) {
    const bad = frame(); mutate(bad); h.put('input', recording([bad]));
    assert.throws(() => h.run('validateRecording(input)'));
  }
  const legacy = frame(); delete legacy.scouts; delete legacy.trucks; delete legacy.extinguishers; delete legacy.fleet_counts;
  h.put('input', recording([legacy]));
  assert.equal(h.run('validateRecording(input).length'), 1);
  assert.deepEqual(h.value('input.frames[0].wind'), [1, 0]);
  h.put('input', recording([])); assert.throws(() => h.run('validateRecording(input)'));
  h.put('input', recording(Array(1501).fill(legacy))); assert.throws(() => h.run('validateRecording(input)'));
});

test('language changes keep active replay controls truthful', async () => {
  const h = await harness(); await h.import([frame(), frame({tick: 1})]);
  h.run("lang='en';applyLang()");
  assert.equal(h.elements.get('replayPlay').textContent, h.run('I18N.en.replayStop'));
  h.run('stopReplay()');
  assert.equal(h.elements.get('replayPlay').textContent, h.run('I18N.en.replayStart'));
});

test('backend errors remain visible after a successful client action', async () => {
  const h = await harness(); h.server.error = 'HappyRobot error';
  await h.run("act('pause')");
  assert.equal(h.elements.get('error').hidden, false);
  assert.match(h.elements.get('error').textContent, /HappyRobot error/);
});

test('network pause failure also blocks server-recording playback', async () => {
  const h = await harness();
  h.handler = request => {if (request.url === '/api/recording') return response(recording([frame()])); throw Error('Network unavailable');};
  await h.elements.get('playRecord').onclick();
  assert.equal(h.run('recordingPlayback===null'), true);
  assert.equal(h.intervals.size, 0);
  assert.match(h.elements.get('error').textContent, /Network unavailable/);
});

test('bad JSON, empty recordings, and oversized uploads never pause the world', async () => {
  for (const body of [null, recording([]), {format: 'wrong-format', frames: [frame()]}]) {
    const h = await harness();
    h.handler = () => body === null ? {ok: true, json: async () => {throw Error('Invalid JSON');}} : response(body);
    await h.elements.get('playRecord').onclick();
    assert.equal(h.requests.filter(request => request.body?.action === 'pause').length, 0);
    assert.equal(h.elements.get('error').hidden, false);
  }
  const h = await harness(); let read = false;
  await h.elements.get('openRecording').onchange({target: {files: [{size: 100000001, text: async () => {read = true; return '';}}], value: 'huge.json'}});
  assert.equal(read, false);
  assert.equal(h.requests.filter(request => request.body?.action === 'pause').length, 0);
});

test('validation scans later frames and retains the 1500-frame bound', async () => {
  const h = await harness(), good = frame();
  h.put('input', recording([good, frame({wind: null})]));
  assert.throws(() => h.run('validateRecording(input)'));
  h.put('input', recording(Array(1500).fill(good)));
  assert.equal(h.run('validateRecording(input).length'), 1500);
});

test('newer file loads win and an invalid replacement retains an existing recording', async () => {
  const h = await harness(), gate = deferred();
  h.put('readSlow', () => gate.promise);
  const old = h.run('loadRecording(readSlow)');
  await h.import([frame({tick: 7})]);
  gate.resolve(recording([frame({tick: 99})])); await old;
  assert.equal(h.run('state.tick'), 7);
  await h.import([frame({wind: null})]);
  assert.equal(h.run('state.tick'), 7);
  assert.equal(h.run('recordingPlayback[0].tick'), 7);
});

test('read-only local replay never posts a decision and Live success resumes polling', async () => {
  const h = await harness(); await h.import([frame({tick: 5})]);
  const before = h.requests.length;
  assert.equal(await h.run("act('decision')"), false);
  assert.equal(h.requests.length, before);
  assert.equal(await h.run("act('live')"), true);
  assert.equal(h.run('recordingPlayback===null'), true);
  const liveCount = h.requests.length; await h.run('poll()');
  assert.equal(h.requests.length, liveCount + 1);
});

test('failed initial and subsequent replay seeks leave no timer running', async () => {
  const h = await harness();
  h.handler = () => response({error: 'Seek failed'}, 400);
  await h.elements.get('replayPlay').onclick();
  assert.equal(h.intervals.size, 0);
  h.handler = () => response(frame({replay: true}));
  await h.elements.get('replayPlay').onclick();
  assert.equal(h.intervals.size, 1);
  h.handler = () => response({error: 'Seek failed'}, 400);
  await [...h.intervals.values()][0].fn();
  assert.equal(h.intervals.size, 0);
  assert.equal(h.run('pending'), false);
});

test('modern fleet arrays provide aliases without inheriting live vehicles or links', async () => {
  const h = await harness(); const recorded = frame({tick: 4});
  delete recorded.drone; delete recorded.truck;
  recorded.workflow_url = '/recorded-workflow';
  await h.import([recorded]);
  assert.equal(h.run('state.drone.drone_id'), 'drone-1');
  assert.equal(h.run('state.truck.truck_id'), 'engine-1');
  assert.equal(h.run('state.workflow_url'), '/workflow');
});

test('state-dependent controls are safe before the first state arrives', async () => {
  const h = await harness(); h.run('state=undefined');
  for (const id of ['play', 'back', 'forward', 'replayPlay']) assert.doesNotThrow(() => h.elements.get(id).onclick());
});

test('post-mortem panel renders oracle grades, gaps, reflections and lessons', async () => {
  const h = await harness();
  assert.match(html, /id="postmortemPanel"/); assert.match(html, /id="postmortem"/);
  const tbody = new Element('tbody');
  h.elements.get('postmortem').querySelector = selector => selector === 'tbody' ? tbody : null;
  const postmortem = {incident_id: 'i', lessons: ['Treat an empty tool result as success.'], patches: [{version_id: 'v2', report_path: '.runtime/patches/x.md'}],
    decisions: [{id: 1, tick: 5, status: 'applied', latency_s: 285.4, decision: {scout_orders: [{drone_id: 'scout-1', command: 'patrol'}], extinguisher_orders: [], truck_orders: [{truck_id: 'engine-1', command: 'continue'}]},
      result: {regret: 100, gap_type: 'execution', best_decision: {scout_orders: [{drone_id: 'scout-1', command: 'evacuate_farm', district_id: 'farm'}], extinguisher_orders: [], truck_orders: []}},
      signals: {loop_detected: true, repeated_tool_calls: {'Scout Agent': 11}}, reflection: 'El agente <b>repitió</b> la llamada.', diagnosis: {proposed_rule: 'Rule'}},
      {id: 2, tick: 21, status: 'applied', latency_s: 40, decision: {}, result: null, signals: null, reflection: null, diagnosis: null}]};
  h.handler = request => request.url === '/api/postmortem' ? response(postmortem) : response(h.server);
  await h.run('renderPostmortem()');
  assert.match(tbody.innerHTML, /gap-execution/); assert.match(tbody.innerHTML, /scout-1: evacuate_farm farm/);
  assert.match(tbody.innerHTML, /&lt;b&gt;repitió&lt;\/b&gt;/); assert.match(tbody.innerHTML, /⚠/);
  assert.match(tbody.innerHTML, /gap-pending/); assert.match(tbody.innerHTML, /analizando/);
  assert.match(h.elements.get('lessons').innerHTML, /empty tool result/);
  assert.match(h.elements.get('patches').innerHTML, /v2/);
});

test('futures panel renders the forecast, district threat, checks and divergence; replay hides it', async () => {
  const h = await harness();
  assert.match(html, /id="forecastPanel"/); assert.match(html, /id="forecastDistricts"/);
  const tbody = new Element('tbody');
  h.elements.get('forecastDistricts').querySelector = selector => selector === 'tbody' ? tbody : null;
  h.run('render(' + JSON.stringify(frame()) + ')');
  assert.match(h.elements.get('forecastSummary').textContent, /Sin pronóstico/);
  const forecast = {issued_at: 16, horizon: 16, branches: 8, dispersion: 0.031, expected_burning_cells: 42.5, believed_burning_cells: 12, consumed: true, valid: true,
    burn_probability: [[40, 20, 0.75]], districts: {farm: {p_fire_within_8: 0.75, p_blocked_or_burnt: 0, expected_distance: 6.2, status_now: 'unwarned', outcomes: {unwarned: 6, evacuating: 2}}}};
  const divergence = {tick: 24, distance: 0.21, threshold: 0.062, divergent: true, what_changed: ['wind changed from [0, -1] to [-3, 0]', '<b>farm</b>: fire within 8 cells']};
  const surprises = [{tick: 20, distance: 0.01, threshold: 0.062, divergent: false}, divergence];
  h.run('render(' + JSON.stringify(frame({tick: 24, called: true, forecast, divergence, surprises})) + ')');
  assert.match(h.elements.get('forecastSummary').textContent, /t\+16 · 8 ramas · dispersión 0.031/); assert.match(h.elements.get('forecastSummary').textContent, /invalidado/);
  assert.match(tbody.innerHTML, /threat-high/); assert.match(tbody.innerHTML, /75%/); assert.match(tbody.innerHTML, /sin aviso \(6\/8\)/);
  assert.match(h.elements.get('divergence').innerHTML, /0.21 > umbral 0.062/); assert.match(h.elements.get('divergence').innerHTML, /&lt;b&gt;farm&lt;\/b&gt;/);
  assert.match(h.elements.get('surprises').innerHTML, /check-held/); assert.match(h.elements.get('surprises').innerHTML, /check-broke/);
  h.run('render(' + JSON.stringify(frame({tick: 24, replay: true, forecast, divergence, surprises})) + ')');
  assert.match(h.elements.get('forecastSummary').textContent, /Sin pronóstico/); assert.equal(tbody.innerHTML, ''); assert.equal(h.elements.get('divergence').innerHTML, '');
});

test('unknown status and radio-source names do not read inherited dictionary properties', async () => {
  const h = await harness();
  assert.equal(h.run("statusText('constructor')"), 'constructor');
  assert.equal(h.run("statusText('__proto__')"), '__proto__');
  h.put('input', frame({history: [{tick: 0, source: 'constructor', message: 'literal'}]}));
  assert.doesNotThrow(() => h.run('render(input)'));
  assert.equal(h.elements.get('trail').children[0].children[1].textContent, 'CONSTRUCTOR');
});

test('learning panel renders the regret curve, the experience sent and the lesson ledger; retire posts an action', async () => {
  const h = await harness();
  assert.match(html, /id="learningPanel"/); assert.match(html, /id="learningEpisodes"/);
  const tbody = new Element('tbody');
  h.elements.get('learningEpisodes').querySelector = selector => selector === 'tbody' ? tbody : null;
  const learning = {
    episodes: [{incident_id: 'aaaaaaaa-1', decisions: 3, graded: 3, mean_regret: 100, judgement_gaps: 0, execution_gaps: 0, surprise_checks: 2, divergences: 1, cases_available: 0, lessons_shown: 0},
      {incident_id: 'bbbbbbbb-2', decisions: 3, graded: 3, mean_regret: 0, judgement_gaps: 0, execution_gaps: 0, surprise_checks: 3, divergences: 0, cases_available: 3, lessons_shown: 1},
      {incident_id: 'cccccccc-3', decisions: 1, graded: 0, mean_regret: null, judgement_gaps: 0, execution_gaps: 0, surprise_checks: 0, divergences: 0, cases_available: 6, lessons_shown: 1}],
    experience: {decision_id: 7, cases: [{decision_id: 1, similarity_distance: 0.08, tick: 2, event_type: 'farmer_call', regret: 100, gap_type: 'none', did: ['drone-1: hold'], oracle_preferred: ['scout-1: evacuate_farm farm'], lesson: 'Warn <b>farm</b> first'}], lessons: []},
    lessons: [{id: 1, rule: 'Warn the downwind farm before scouting.', active: 1, uses: 4, regret_with: 0, regret_without: 100},
      {id: 2, rule: 'Old rule', active: 0, uses: 5, regret_with: 120, regret_without: 10, retired_reason: 'regret with lesson 120 vs without 10 over 5 decisions'}]};
  h.handler = request => request.url === '/api/learning' ? response(learning) : response(h.server);
  await h.run('renderLearning()');
  assert.match(h.elements.get('learningCurve').innerHTML, /primer episodio 100 → último 0/); assert.match(h.elements.get('learningCurve').innerHTML, /trend-down/); assert.match(h.elements.get('learningCurve').innerHTML, /<svg/);
  assert.equal((tbody.innerHTML.match(/<tr/g) || []).length, 2); assert.match(tbody.innerHTML, /aaaaaaaa/); assert.doesNotMatch(tbody.innerHTML, /cccccccc/);
  assert.match(h.elements.get('experienceUsed').innerHTML, /d=0.08 · t=2 · farmer_call · regret 100/); assert.match(h.elements.get('experienceUsed').innerHTML, /evacuate_farm/); assert.match(h.elements.get('experienceUsed').innerHTML, /&lt;b&gt;farm&lt;\/b&gt;/);
  assert.match(h.elements.get('lessonLedger').innerHTML, /lesson-retired/); assert.match(h.elements.get('lessonLedger').innerHTML, /regret con 0 \/ sin 100/); assert.match(h.elements.get('lessonLedger').innerHTML, /data-lesson="1" data-active="0"/);
  h.handler = request => request.url === '/api/learning' ? response(learning) : response(h.server);
  h.elements.get('lessonLedger').onclick({target: {dataset: {lesson: '2', active: '1'}}});
  await flush();
  const action = h.requests.find(request => request.body?.action === 'lesson');
  assert.deepEqual(action.body, {action: 'lesson', id: 2, active: true});
  h.handler = request => request.url === '/api/learning' ? response({episodes: [], lessons: [], experience: null}) : response(h.server);
  await h.run('renderLearning()');
  assert.match(h.elements.get('learningCurve').textContent, /Sin episodios/); assert.equal(tbody.innerHTML, ''); assert.equal(h.elements.get('experienceUsed').innerHTML, '');
});

test('experience and results cards choose distinct views and keyboard buttons expose selection', async () => {
  const h = await harness();
  assert.equal(h.run('I18N.es.adapt'), 'Adaptación');
  assert.equal(h.run('I18N.en.adapt'), 'Adaptation');
  h.elements.get('adaptMemory').click();
  assert.equal(h.elements.get('learningExperience').hidden, false);
  assert.equal(h.elements.get('learningResults').hidden, true);
  h.elements.get('showResults').click();
  assert.equal(h.elements.get('learningExperience').hidden, true);
  assert.equal(h.elements.get('showResults').getAttribute('aria-pressed'), 'true');
  h.elements.get('adaptLearn').click();
  assert.equal(h.elements.get('learningResults').hidden, false);
  h.elements.get('showExperience').click();
  assert.equal(h.elements.get('showResults').getAttribute('aria-pressed'), 'false');
});

test('case labels expose fleet differences safely and unchanged polling preserves rendered nodes', async () => {
  const h = await harness(), element = h.elements.get('experienceUsed');
  let markup = '', writes = 0;
  Object.defineProperty(element, 'innerHTML', {get: () => markup, set: value => {markup = value; writes++;}});
  const learning = {episodes: [], lessons: [], experience: {cases: [{decision_id: 2, regret: 10,
    matching_labels: ['wind:north', '<img src=x onerror=alert(1)>'],
    differing_labels: {current_only: ['fleet:scouts:1'], case_only: ['fleet:scouts:2']},
    did: ['scout-1: hold'], oracle_preferred: ['scout-1: patrol']}]}};
  h.handler = () => response(learning);
  await h.run('renderLearning()'); await h.run('renderLearning()');
  assert.equal(writes, 1);
  assert.match(markup, /1 explorador/); assert.match(markup, /2 exploradores/);
  assert.match(markup, /&lt;img src=x onerror=alert\(1\)&gt;/);
  assert.doesNotMatch(markup, /<img/);
  h.run("lang='en'; applyLang()");
  assert.match(markup, /1 scout</); assert.match(markup, /2 scouts</);
  assert.match(markup, /Wind toward north/);
  assert.equal(writes, 2);
  h.handler = () => {throw Error('offline');};
  await h.run('renderLearning()');
  assert.equal(markup, '');
  assert.equal(h.elements.get('learningStatus').hidden, false);
  h.handler = () => response(learning);
  await h.run('renderLearning()');
  assert.match(markup, /Case #2/);
  assert.equal(h.elements.get('learningStatus').hidden, true);
});

test('a delayed learning response uses current language and renders accessible finite results only', async () => {
  const h = await harness(), gate = deferred();
  h.handler = () => gate.promise;
  const pending = h.run('renderLearning()');
  h.run("lang='en'; applyLang()");
  gate.resolve(response({episodes: [{graded: 1, mean_regret: 0}, {graded: 1, mean_regret: null}], lessons: [],
    experience: {cases: [{decision_id: 9, regret: 0, did: ['scout-1: hold']}]}}));
  await pending;
  assert.match(h.elements.get('experienceUsed').innerHTML, /This record has no context labels/);
  assert.match(h.elements.get('experienceUsed').innerHTML, /No better alternative recorded/);
  const chart = h.elements.get('learningCurve').innerHTML;
  assert.match(chart, /role="img" aria-label="Mean cost gap: 0/);
  assert.doesNotMatch(chart, /NaN|null/);
  assert.match(chart, /Evaluated decisions/);
});
