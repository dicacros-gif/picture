const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { EventEmitter } = require('node:events');
const { defaults, normalizeBlog, migratePrompt, SettingsStore, BackendRunner, BlogController } = require('../blog-runtime');

function directory(t) {
  const value = fs.mkdtempSync(path.join(os.tmpdir(), 'blog-mac-runtime-'));
  t.after(() => fs.rmSync(value, { recursive: true, force: true }));
  return value;
}
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
class Timers {
  constructor() { this.active = new Map(); this.all = []; }
  setTimeout = (fn, delay) => {
    const timer = { fn, delay, unref() {} };
    this.active.set(timer, timer); this.all.push(timer); return timer;
  };
  clearTimeout = timer => { this.active.delete(timer); };
  fire(timer) { this.active.delete(timer); timer.fn(); }
}
function childProcess() {
  const child = new EventEmitter();
  Object.assign(child, { pid: 2345, exitCode: null, stdout: new EventEmitter(), stderr: new EventEmitter(), stdin: new EventEmitter(), killed: [] });
  child.stdin.end = input => { child.input = input; };
  child.kill = signal => { child.killed.push(signal); return true; };
  child.close = (code = 0, signal = null) => { child.exitCode = code; child.emit('close', code, signal); };
  return child;
}
function runnerFixture() {
  const timers = new Timers(), children = [], progress = [], signals = [];
  const runner = new BackendRunner({ executable: '/fake/blog-engine', dataDir: '/fake/Blog', platform: 'darwin', timers,
    killProcess: (...args) => signals.push(args), emit: event => progress.push(event),
    spawnProcess: (command, args, options) => {
      const child = childProcess(); Object.assign(child, { command, args, options }); children.push(child); return child;
    } });
  return { runner, children, timers, progress, signals };
}
function controllerFixture(t) {
  const store = new SettingsStore(directory(t), '원래 사용자 프롬프트');
  const timers = new Timers(), events = [], calls = [];
  let now = Date.UTC(2026, 8, 13), collect = async (keyword, automatic) => ({ keywords: [keyword, '연관어'], automatic }), ensure = async () => {};
  let invoke = async () => ({ status: 'local', message: '저장 완료' });
  const backend = { invoke: async (...args) => { calls.push(args); return invoke(...args); }, stopped: 0, terminate() { this.stopped++; } };
  const controller = new BlogController({ store, backend, timers, now: () => now, emit: event => events.push(event),
    collect: (...args) => collect(...args), ensureBrowser: (...args) => ensure(...args) });
  return { store, timers, events, calls, backend, controller, setNow: value => { now = value; },
    setCollect: value => { collect = value; }, setEnsure: value => { ensure = value; }, setInvoke: value => { invoke = value; } };
}

test('settings retain prompts, stages, models, layout and mode across restart', t => {
  const dir = directory(t), first = new SettingsStore(dir, 'default');
  first.set({ naverBlogId: 'my-blog', blog: { intervalHours: '6', mode: 'publish', keyword: ' 한글 주제 ',
    prompts: [{ id: 'one', name: '설명', text: '이게 무슨 말일까요?\n사용자 문체' }, { id: 'two', name: '비교', text: '비교해요.' }], selectedPromptId: 'two',
    stages: [{ provider: 'antigravity', role: '팩트·최신 정보 보강', model: 'model-custom' }], progressHeight: 299, progressCollapsed: true } });
  const next = new SettingsStore(dir, 'new-default').get();
  assert.equal(next.naverBlogId, 'my-blog');
  assert.equal(next.blog.intervalHours, 6); assert.equal(next.blog.mode, 'publish');
  assert.equal(next.blog.keyword, '한글 주제'); assert.equal(next.blog.selectedPromptId, 'two');
  assert.equal(next.blog.prompts[0].text, '이게 무슨 말일까요?\n사용자 문체');
  assert.deepEqual(next.blog.stages, [{ provider: 'antigravity', role: '팩트·최신 정보 보강', model: 'model-custom' }]);
  assert.equal(next.blog.progressHeight, 299); assert.equal(next.blog.progressCollapsed, true);
});

test('saved app prompt upgrades once while preserving user presets', t => {
  const latest = '오늘 기본 지침\n\n[맥 공통 글쓰기 규칙 · 2026-09-13 v3]\n최신 이미지 규칙';
  const legacyDefault = '사용자 앞부분\n아주 약한 미세 필름 그레인만\n첫 사진은 8자 안팎, 최대 12자\n위쪽 32%는 글자가 잘 읽히도록';
  const upgradedDefault = migratePrompt(legacyDefault, latest);
  assert.match(upgradedDefault, /^사용자 앞부분/);
  assert.match(upgradedDefault, /\[맥 공통 글쓰기 규칙 · 2026-09-13 v3\]/);
  const custom = '내가 직접 저장한 짧은 글쓰기 지침';
  assert.equal(migratePrompt(custom, latest), custom);

  const marked = '직접 작성한 앞 지침\n\n[이미지 문구 최신 규칙 · 2026-09-13]\n빨강은 쓰지 않습니다.';
  const migrated = migratePrompt(marked, latest);
  assert.match(migrated, /\[맥 공통 글쓰기 규칙 · 2026-09-13 v3\]/);
  assert.match(migrated, /형광 녹색/);
  assert.match(migrated, /형광 빨간색/);
  assert.match(migrated, /반투명 검정 배경/);
  assert.equal(migratePrompt(migrated, latest), migrated);
});

test('prompt migration is written to settings and remains after restart', t => {
  const dir = directory(t), first = new SettingsStore(dir, '새 기본 프롬프트');
  fs.writeFileSync(first.file, JSON.stringify({ blog: { prompts: [{ id: 'default', name: '기본 글쓰기',
    text: '옛 규칙\n아주 약한 미세 필름 그레인만\n8자 안팎, 최대 12자\n위쪽 32%는 글자가 잘 읽히도록' }],
    selectedPromptId: 'default' } }), 'utf8');
  const upgraded = first.get().blog.prompts[0].text;
  assert.match(upgraded, /^옛 규칙/);
  assert.match(upgraded, /\[맥 공통 글쓰기 규칙 · 2026-09-13 v3\]/);
  const persisted = JSON.parse(fs.readFileSync(first.file, 'utf8'));
  assert.equal(persisted.blog.prompts[0].text, upgraded);
  assert.equal(new SettingsStore(dir, '다른 기본값').get().blog.prompts[0].text, upgraded);
});

test('delayed autosave cannot turn automation back on after Stop', t => {
  const store = new SettingsStore(directory(t), 'default');
  store.set({ blog: { automationEnabled: true } }, { automation: true });
  const stale = store.get();
  store.set({ blog: { automationEnabled: false } }, { automation: true });
  store.set({ blog: { ...stale.blog, keyword: 'new keyword' } });
  assert.equal(store.get().blog.automationEnabled, false);
  assert.equal(store.get().blog.keyword, 'new keyword');
});

test('only explicit toggle changes automation, and valid on survives restart', t => {
  const store = new SettingsStore(directory(t), 'default');
  store.set({ blog: { automationEnabled: true } });
  assert.equal(store.get().blog.automationEnabled, false);
  store.set({ blog: { automationEnabled: true } }, { automation: true });
  assert.equal(new SettingsStore(path.dirname(store.file), '').get().blog.automationEnabled, true);
  store.set({ blog: { automationEnabled: false, intervalHours: 2 } });
  assert.equal(store.get().blog.automationEnabled, true);
});

test('corrupt or absent primary recovers preferences with automation off', t => {
  const store = new SettingsStore(directory(t), 'default');
  store.set({ blog: { keyword: '이전 주제', automationEnabled: true } }, { automation: true });
  store.set({ blog: { keyword: '새 주제' } });
  fs.writeFileSync(store.file, '{incomplete');
  assert.equal(store.get().blog.keyword, '이전 주제');
  assert.equal(store.get().blog.automationEnabled, false);
  fs.unlinkSync(store.file);
  assert.equal(store.get().blog.keyword, '이전 주제');
  assert.equal(store.get().blog.automationEnabled, false);
});

test('structurally invalid settings recover; unusable files remain untouched', t => {
  const store = new SettingsStore(directory(t), 'default');
  store.set({ blog: { keyword: 'good' } }); store.set({ blog: { keyword: 'new' } });
  fs.writeFileSync(store.file, 'null');
  assert.equal(store.get().blog.keyword, 'good');
  fs.writeFileSync(`${store.file}.last-good`, '[]');
  assert.throws(() => store.get(), /기존 파일을 보존/);
  assert.equal(fs.readFileSync(store.file, 'utf8'), 'null');
  assert.equal(fs.readFileSync(`${store.file}.last-good`, 'utf8'), '[]');
});

test('settings patch rejects malformed objects and supports old BOM settings', t => {
  const store = new SettingsStore(directory(t), 'default');
  for (const value of [null, [], 'bad', { blog: null }, { blog: [] }]) assert.throws(() => store.set(value), /형식/);
  fs.writeFileSync(store.file, '\uFEFF' + JSON.stringify({ blogId: 'legacy' }));
  assert.equal(store.get().blogId, 'legacy');
  assert.equal(store.get().blog.prompts[0].text, 'default');
});

test('normalization allows only intervals 1–6 and caps models, presets and stage count', () => {
  for (let value = 1; value <= 6; value++) assert.equal(normalizeBlog({ intervalHours: String(value) }).intervalHours, value);
  for (const value of [0, 7, -1, 1.5, 'NaN', Infinity]) assert.equal(normalizeBlog({ intervalHours: value }).intervalHours, 1);
  assert.deepEqual(normalizeBlog(null), defaults());
  const value = normalizeBlog({ stages: Array(8).fill({ provider: 'unknown', role: 'unknown', model: 'a'.repeat(500) }),
    prompts: [{ id: 'id', name: 'one', text: 'one' }, { id: 'id', name: 'two', text: 'two' }], selectedPromptId: 'missing', googleReferenceCount: 2.7 });
  assert.equal(value.stages.length, 4); assert.equal(value.stages[0].model.length, 120);
  assert.equal(value.stages[0].provider, 'chatgpt'); assert.equal(value.prompts.length, 1);
  assert.equal(value.selectedPromptId, 'id'); assert.equal(value.googleReferenceCount, 3);
});

test('runner decodes UTF-8 split inside Korean and accepts final line without newline', async () => {
  const f = runnerFixture();
  const pending = f.runner.invoke('run', { keyword: '한글' }, { work: true });
  const child = f.children[0], output = Buffer.from(JSON.stringify({ event: 'result', result: { title: '한글 제목' } }));
  const split = output.indexOf(Buffer.from('한')) + 1;
  child.stdout.emit('data', output.subarray(0, split)); child.stdout.emit('data', output.subarray(split));
  child.close();
  assert.deepEqual(await pending, { title: '한글 제목' });
  assert.deepEqual(JSON.parse(child.input), { keyword: '한글' });
  assert.equal(child.options.shell, false); assert.equal(child.options.detached, true);
  assert.equal(f.runner.work, null); assert.equal(f.timers.active.size, 0);
});

test('runner ignores unrelated output and passes only sanitized progress', async () => {
  const f = runnerFixture(), pending = f.runner.invoke('status');
  f.children[0].stdout.emit('data', Buffer.from('library line\nnull\n' + JSON.stringify({ event: 'progress', stage: 'writing', message: 'Bearer abc-secret' }) + '\n' + JSON.stringify({ event: 'result', result: {} }) + '\n'));
  f.children[0].close(); await pending;
  assert.deepEqual(f.progress, [{ message: 'Bearer [가림]', stage: 'writing' }]);
});

test('runner rejects error events, nonzero exits and signal exits even with a result', async () => {
  for (const scenario of ['error', 'code', 'signal']) {
    const f = runnerFixture(), pending = f.runner.invoke('run', {}, { work: true });
    if (scenario === 'error') f.children[0].stdout.emit('data', Buffer.from('{' + '"event":"error","code":"auth","message":"로그인 필요"}\n'));
    f.children[0].stdout.emit('data', Buffer.from('{"event":"result","result":{"message":"success"}}\n'));
    f.children[0].close(scenario === 'code' ? 4 : scenario === 'signal' ? null : 0, scenario === 'signal' ? 'SIGTERM' : null);
    await assert.rejects(pending, scenario === 'error' ? /로그인 필요/ : /작성 엔진 종료/);
    assert.equal(f.runner.work, null);
  }
});

test('runner rejects duplicate results instead of silently replacing publication receipt', async () => {
  const f = runnerFixture(), pending = f.runner.invoke('run', {}, { work: true });
  f.children[0].stdout.emit('data', Buffer.from('{"event":"result","result":{"url":"one"}}\n{"event":"result","result":{"url":"two"}}\n'));
  await assert.rejects(pending, /중복 완료/); assert.deepEqual(f.children[0].killed, ['SIGTERM']);
  f.children[0].close(1);
});

test('runner timeout keeps stop escalation and work lock until process closes', async () => {
  const f = runnerFixture(), pending = f.runner.invoke('run', {}, { work: true });
  f.timers.fire(f.timers.all[0]);
  await assert.rejects(pending, /제한 시간/);
  await assert.rejects(f.runner.invoke('run', {}, { work: true }), /이미 글 작성/);
  assert.equal(f.runner.work, f.children[0]);
  const escalation = f.children[0].stopTimer;
  assert.equal(f.timers.active.has(escalation), true);
  f.timers.fire(escalation); assert.deepEqual(f.signals, [[-2345, 'SIGKILL']]);
  f.children[0].close(null, 'SIGKILL'); assert.equal(f.runner.work, null);
});

test('runner cancellation is idempotent and close clears its escalation', async () => {
  const f = runnerFixture(), pending = f.runner.invoke('run', {}, { work: true });
  f.runner.terminate(); f.runner.terminate();
  assert.deepEqual(f.children[0].killed, ['SIGTERM']);
  f.children[0].close(null, 'SIGTERM'); await assert.rejects(pending, /SIGTERM/);
  assert.equal(f.timers.active.size, 0);
});

test('runner limits complete-line output as well as partial line size', async () => {
  const f = runnerFixture(), pending = f.runner.invoke('run', {}, { work: true });
  const line = Buffer.from('x'.repeat(1024 * 1024 - 1) + '\n');
  for (let i = 0; i < 33; i++) f.children[0].stdout.emit('data', line);
  await assert.rejects(pending, /제한 크기/); assert.deepEqual(f.children[0].killed, ['SIGTERM']);
  f.children[0].close(1);
});

test('runner fails cleanly for missing results and process launch errors', async () => {
  for (const fail of [false, true]) {
    const f = runnerFixture(), pending = f.runner.invoke('status');
    if (fail) f.children[0].emit('error', new Error('not executable'));
    f.children[0].close(fail ? -2 : 0);
    await assert.rejects(pending, fail ? /실행 실패/ : /결과 없이/);
    assert.equal(f.timers.active.size, 0);
  }
});

test('manual keyword research uses frozen prompt/model snapshot and chosen completion mode', async t => {
  const f = controllerFixture(t), gate = deferred();
  f.setCollect(() => gate.promise);
  f.controller.start({ keyword: '  맥북 배터리  ', mode: 'local' });
  f.store.set({ blog: { prompts: [{ id: 'next', name: 'new', text: '다음 글 규칙' }], selectedPromptId: 'next',
    stages: [{ provider: 'chatgpt', role: '작성', model: 'new-model' }], mode: 'publish' } });
  gate.resolve({ relatedKeywords: ['교체 비용'], settings: { bad: true }, keyword: 'override', automatic: true });
  await f.controller.running;
  const first = f.calls[0][1];
  assert.equal(first.keyword, '맥북 배터리'); assert.equal(first.automatic, false);
  assert.equal(first.settings.blog.mode, 'local');
  assert.equal(first.settings.blog.prompts[0].text, '원래 사용자 프롬프트');
  assert.equal(first.settings.blog.stages[0].model, '');
  assert.deepEqual(first.relatedKeywords, ['교체 비용']);
  f.controller.start({ keyword: '다음 주제' }); await f.controller.running;
  assert.equal(f.calls[1][1].settings.blog.prompts[0].text, '다음 글 규칙');
  assert.equal(f.calls[1][1].settings.blog.stages[0].model, 'new-model');
  assert.equal(f.calls[1][1].settings.blog.mode, 'publish');
});

test('manual validation and no overlapping work', async t => {
  const f = controllerFixture(t), gate = deferred();
  for (const keyword of ['', ' ', 'x'.repeat(161)]) assert.throws(() => f.controller.start({ keyword }), /1~160/);
  assert.throws(() => f.controller.start({ keyword: 'ok', mode: 'bad' }), /완료 동작/);
  f.setCollect(() => gate.promise); f.controller.start({ keyword: 'one' });
  assert.throws(() => f.controller.start({ keyword: 'two' }), /이미 글 작성/);
  gate.resolve({}); await f.controller.running;
  assert.equal(f.calls.length, 1); assert.equal(f.controller.state().busy, false);
});

test('automation defaults off and enabled launch starts immediately once', async t => {
  const f = controllerFixture(t), gate = deferred();
  f.controller.initialize(); assert.equal(f.controller.busy, false);
  f.setCollect(() => gate.promise);
  f.controller.setAutomation(true); f.controller.initialize();
  assert.equal(f.controller.busy, true);
  gate.resolve({}); await f.controller.running;
  assert.equal(f.calls.length, 1); assert.equal(f.calls[0][1].automatic, true);
  assert.equal(f.controller.timer.delay, 3600000);
  f.controller.setAutomation(true); assert.equal(f.calls.length, 1);
});

test('interval change reschedules from last start and stale timer cannot start', async t => {
  const f = controllerFixture(t);
  f.controller.setAutomation(true); await f.controller.running;
  const first = f.controller.timer, completedAt = Date.UTC(2026, 8, 13);
  f.setNow(completedAt + 15 * 60000);
  f.store.set({ blog: { intervalHours: 2 } }); f.controller.settingsChanged();
  assert.equal(f.controller.nextRunAt, new Date(completedAt + 7200000).toISOString());
  assert.equal(f.controller.timer.delay, 105 * 60000);
  first.fn(); assert.equal(f.calls.length, 1);
  f.controller.stop(); f.controller.timer?.fn(); first.fn();
  assert.equal(f.calls.length, 1); assert.equal(f.controller.nextRunAt, null); assert.equal(f.timers.active.size, 0);
});

test('stopping during research avoids browser and backend then permits manual use', async t => {
  const f = controllerFixture(t), gate = deferred(); let browsers = 0;
  f.setCollect(() => gate.promise); f.setEnsure(async () => { browsers++; });
  f.controller.setAutomation(true); f.controller.stop(); gate.resolve({});
  await f.controller.running;
  assert.equal(f.calls.length, 0); assert.equal(browsers, 0); assert.equal(f.backend.stopped, 1);
  assert.equal(f.controller.state().status, 'stopped'); assert.equal(f.controller.state().automationEnabled, false);
  f.setCollect(async () => ({})); f.controller.start({ keyword: '수동 글' }); await f.controller.running;
  assert.equal(f.calls.length, 1); assert.equal(f.controller.timer, null);
});

test('stopping during browser check prevents paid generation', async t => {
  const f = controllerFixture(t), gate = deferred();
  f.setEnsure(() => gate.promise); f.controller.start({ keyword: 'topic' });
  await Promise.resolve(); f.controller.stop(); gate.resolve(); await f.controller.running;
  assert.equal(f.calls.length, 0); assert.equal(f.controller.state().status, 'stopped');
});

test('failed run retains actionable error and never displays an earlier success result', async t => {
  const f = controllerFixture(t);
  f.controller.start({ keyword: 'success' }); await f.controller.running;
  assert.equal(f.controller.state().status, 'complete');
  f.setInvoke(async () => { throw new Error('CLI 로그인 필요'); });
  f.controller.start({ keyword: 'failure' }); await f.controller.running;
  assert.equal(f.controller.state().lastResult, null); assert.equal(f.controller.state().status, 'error');
  assert.equal(f.controller.state().lastError, 'CLI 로그인 필요'); assert.equal(f.controller.state().busy, false);
});

test('local save can continue without browser, publish cannot', async t => {
  const f = controllerFixture(t);
  f.setEnsure(async () => { throw new Error('브라우저 연결 실패'); });
  f.controller.start({ keyword: 'local', mode: 'local' }); await f.controller.running;
  assert.equal(f.calls.length, 1); assert.equal(f.controller.status, 'complete');
  f.controller.start({ keyword: 'publish', mode: 'publish' }); await f.controller.running;
  assert.equal(f.calls.length, 1); assert.equal(f.controller.status, 'error');
});

test('automatic failure schedules next run without overlapping or changing settings', async t => {
  const f = controllerFixture(t);
  f.store.set({ blog: { intervalHours: 6 } });
  f.setInvoke(async () => { throw new Error('검수 연결 실패'); });
  f.controller.setAutomation(true); await f.controller.running;
  assert.equal(f.controller.status, 'error'); assert.equal(f.controller.timer.delay, 6 * 3600000);
  assert.equal(f.calls.length, 1); assert.equal(f.controller.state().automationEnabled, true);
  f.controller.stop(); assert.equal(f.controller.timer, null);
});


test('a 40-minute article leaves 20 minutes until the next hourly start', async t => {
  const f = controllerFixture(t);
  f.setInvoke(async () => {
    f.setNow(Date.UTC(2026, 8, 13) + 40 * 60000);
    return { status: 'local', saved: true, published: false };
  });
  f.controller.setAutomation(true); await f.controller.running;
  assert.equal(f.controller.timer.delay, 20 * 60000);
});
