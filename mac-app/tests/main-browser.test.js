const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { EventEmitter } = require('node:events');

// Exercise the actual Electron main-process module without launching a browser,
// opening a user's profile, or invoking any external authentication flow.
const source = fs.readFileSync(path.join(__dirname, '..', 'main.js'), 'utf8');
const endpoint = 'ws://127.0.0.1:9449/devtools/browser/test-session';
const profile = '/data/Blog/naver-whale-profile-v2';
const ownerFile = '/data/Blog/whale-debug-owner.json';
const refused = () => Object.assign(new Error('fetch failed'), { cause: { code: 'ECONNREFUSED' } });
const response = (webSocketDebuggerUrl = endpoint) => ({ ok: true, json: async () => ({ webSocketDebuggerUrl }) });

function fixture({ owner, fetch: fetchImpl = async () => response(), firstInstance = true, smokeTest = false } = {}) {
  const files = new Map(), children = [], requests = [], sockets = [], timers = new Map();
  const appEvents = new Map(), handlers = new Map();
  const appCalls = { locks: 0, quits: 0, ready: 0, paths: [] };
  if (owner !== undefined) files.set(ownerFile, typeof owner === 'string' ? owner : JSON.stringify(owner));
  class Socket {
    constructor(url) { this.url = url; this.readyState = 0; this.sent = []; sockets.push(this); }
    open() { this.readyState = 1; this.onopen?.(); }
    send(value) { if (this.sendError) throw this.sendError; this.sent.push(JSON.parse(value)); }
    close() { this.readyState = 3; this.onclose?.(); }
    reply(id, result) { this.onmessage?.({ data: JSON.stringify({ id, result }) }); }
  }
  const electron = {
    app: {
      getPath: key => ({ userData: '/data/Blog', appData: '/data', home: '/home/user', temp: '/tmp' }[key]),
      setPath: (...args) => appCalls.paths.push(args),
      requestSingleInstanceLock: () => { appCalls.locks++; return firstInstance; },
      whenReady: () => { appCalls.ready++; return { then() {} }; },
      on: (event, fn) => appEvents.set(event, fn), quit: () => { appCalls.quits++; }, isReady: () => false
    },
    ipcMain: { handle: (name, handler) => handlers.set(name, handler) }
  };
  const mocks = {
    electron,
    fs: {
      existsSync: file => file === '/Applications/Whale.app/Contents/MacOS/Whale' || files.has(file),
      readFileSync: file => { if (!files.has(file)) throw Object.assign(new Error('missing'), { code: 'ENOENT' }); return files.get(file); },
      mkdirSync() {}
    },
    path: path.posix,
    child_process: {
      execFileSync: () => '',
      spawn: (...args) => {
        const child = Object.assign(new EventEmitter(), { pid: 4321, exitCode: null, signalCode: null, args });
        children.push(child); return child;
      }
    },
    './blog-runtime': { atomicJson: (file, value) => files.set(file, JSON.stringify(value)) }
  };
  const context = vm.createContext({
    require: name => mocks[name] || require(name),
    process: { platform: 'darwin', argv: smokeTest ? ['--smoke-test'] : [], pid: 123 }, __dirname: '/app', Buffer, URL, AbortSignal, WebSocket: Socket,
    fetch: (...args) => { requests.push(args); return fetchImpl(...args); },
    setTimeout: (fn, delay) => { const timer = { fn, delay }; timers.set(timer, timer); return timer; },
    clearTimeout: timer => timers.delete(timer)
  });
  vm.runInContext(`${source}\nglobalThis.browserTests = { ensureWhale, CdpPage, openWhalePage, setWindow: value => { mainWindow = value; }, setBackend: value => { backend = value; } };`, context);
  return { ...context.browserTests, files, children, requests, sockets, timers, appEvents, appCalls, handlers };
}

test('existing browser with a matching profile and session is reused', async () => {
  const f = fixture({ owner: { profile, webSocket: endpoint } });
  await f.ensureWhale();
  assert.equal(f.children.length, 0);
  assert.equal(f.requests[0][1].redirect, 'error');
  assert.ok(f.requests[0][1].signal);
});

for (const [name, owner] of [['missing', undefined], ['malformed', '{'], ['null', 'null'],
  ['wrong profile', { profile: '/somewhere/else', webSocket: endpoint }],
  ['old session', { profile, webSocket: `${endpoint}-old` }]]) {
  test(`existing browser with ${name} ownership fails without spawning or rewriting it`, async () => {
    const f = fixture({ owner });
    const prior = f.files.get(ownerFile);
    await assert.rejects(f.ensureWhale(), /소유 정보|다른 프로그램/);
    assert.equal(f.children.length, 0);
    assert.equal(f.files.get(ownerFile), prior);
  });
}

test('HTTP errors, invalid JSON and non-local socket addresses do not permit ownership', async () => {
  for (const fetchImpl of [async () => ({ ok: false }), async () => ({ ok: true, json: async () => { throw new Error('bad json'); } }),
    async () => response('ws://example.com:9449/devtools/browser/test'), async () => { throw new Error('timeout'); }]) {
    const f = fixture({ fetch: fetchImpl });
    await assert.rejects(f.ensureWhale());
    assert.equal(f.children.length, 0);
    assert.equal(f.files.has(ownerFile), false);
  }
});

test('concurrent requests launch only one dedicated browser after a refused connection', async () => {
  let calls = 0;
  const f = fixture({ fetch: async () => { if (++calls === 1) throw refused(); return response(); } });
  await Promise.all([f.ensureWhale(), f.ensureWhale(), f.ensureWhale()]);
  assert.equal(f.children.length, 1);
  assert.equal(calls, 2);
  assert.ok(f.children[0].args[1].includes(`--user-data-dir=${profile}`));
  assert.deepEqual(JSON.parse(f.files.get(ownerFile)), { profile, webSocket: endpoint, pid: 4321 });
});

test('failed ownership check releases the launch lock for an explicit retry', async () => {
  const f = fixture();
  await assert.rejects(f.ensureWhale(), /소유 정보/);
  f.files.set(ownerFile, JSON.stringify({ profile, webSocket: endpoint }));
  await f.ensureWhale();
  assert.equal(f.requests.length, 2);
});

test('socket close before opening rejects immediately and clears connection timer', async () => {
  const f = fixture(), page = new f.CdpPage(endpoint);
  const result = assert.rejects(page.send('Page.enable'), /연결이 닫혔습니다/);
  f.sockets[0].close();
  await result;
  assert.equal(f.timers.size, 0);
  assert.equal(page.pending.size, 0);
});

test('closed sockets reject new requests without waiting for the command timeout', async () => {
  const f = fixture(), page = new f.CdpPage(endpoint), socket = f.sockets[0];
  socket.open(); socket.close();
  await assert.rejects(page.send('Page.enable'), /연결이 닫혔습니다/);
  assert.equal(f.timers.size, 0);
});

test('synchronous socket send failure does not leave an unresolved request or timer', async () => {
  const f = fixture(), page = new f.CdpPage(endpoint), socket = f.sockets[0];
  socket.open(); socket.sendError = new Error('send failed');
  await assert.rejects(page.send('Page.enable'), /send failed/);
  assert.equal(f.timers.size, 0);
  assert.equal(page.pending.size, 0);
});

test('closing an open socket rejects pending commands and clears their timers', async () => {
  const f = fixture(), page = new f.CdpPage(endpoint), socket = f.sockets[0];
  socket.open();
  const result = assert.rejects(page.send('Page.enable'), /연결이 닫혔습니다/);
  await Promise.resolve(); socket.close();
  await result;
  assert.equal(f.timers.size, 0);
  assert.equal(page.pending.size, 0);
});

test('tab creation uses a deadline and rejects remote debugging sockets', async () => {
  let calls = 0;
  const f = fixture({ owner: { profile, webSocket: endpoint }, fetch: async () => {
    return ++calls === 1 ? response() : response('ws://other-host:9449/devtools/page/a');
  } });
  await assert.rejects(f.openWhalePage('about:blank'), /로컬 포트/);
  assert.equal(f.sockets.length, 0);
  assert.equal(f.requests[1][1].method, 'PUT');
  assert.equal(f.requests[1][1].redirect, 'error');
  assert.ok(f.requests[1][1].signal);
});

test('a second application process quits before initializing settings or automation', () => {
  const f = fixture({ firstInstance: false });
  assert.equal(f.appCalls.locks, 1);
  assert.equal(f.appCalls.quits, 1);
  assert.equal(f.appCalls.ready, 0);
  assert.equal(f.appEvents.has('second-instance'), false);
});

test('the first application instance restores and focuses its existing window', () => {
  const f = fixture(), calls = [];
  f.setWindow({ isDestroyed: () => false, isMinimized: () => true,
    restore: () => calls.push('restore'), show: () => calls.push('show'), focus: () => calls.push('focus') });
  f.appEvents.get('second-instance')();
  assert.deepEqual(calls, ['restore', 'show', 'focus']);
});

test('smoke tests use a separate temporary profile and do not acquire the live app lock', () => {
  const f = fixture({ smokeTest: true });
  assert.equal(f.appCalls.locks, 0);
  assert.equal(f.appCalls.ready, 1);
  assert.deepEqual(f.appCalls.paths, [['userData', '/tmp/blog-mac-smoke-123']]);
});

test('login IPC forwards an explicit ChatGPT device-code choice without changing ordinary login', async () => {
  const f = fixture(), calls = [];
  f.setBackend({ invoke: (...args) => { calls.push(args); return { status: 'login_opened' }; } });
  const login = f.handlers.get('blog-cli-login');
  for (const provider of ['chatgpt', 'claude', 'antigravity']) await login(null, provider);
  await login(null, 'chatgpt', { deviceAuth: true });
  assert.deepEqual(JSON.parse(JSON.stringify(calls)), [
    ['login', { provider: 'chatgpt', deviceAuth: false }],
    ['login', { provider: 'claude', deviceAuth: false }],
    ['login', { provider: 'antigravity', deviceAuth: false }],
    ['login', { provider: 'chatgpt', deviceAuth: true }]
  ]);
});

test('login IPC rejects malformed and non-ChatGPT device-code requests before opening Terminal', () => {
  const f = fixture(), calls = [];
  f.setBackend({ invoke: (...args) => calls.push(args) });
  const login = f.handlers.get('blog-cli-login');
  for (const [provider, options] of [['claude', { deviceAuth: true }], ['antigravity', { deviceAuth: true }],
    ['chatgpt', { deviceAuth: 'true' }], ['chatgpt', null], ['chatgpt', []], ['unknown', {}]]) {
    assert.throws(() => login(null, provider, options), /ChatGPT CLI/);
  }
  assert.equal(calls.length, 0);
});
