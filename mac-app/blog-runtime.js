// Mac-only controls and durable preferences. The shared Python engine owns writing and verification.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { spawn } = require('node:child_process');
const { StringDecoder } = require('node:string_decoder');

const ROLES = ['작성', '교차 검수', '팩트·최신 정보 보강', '문체 다듬기'];
const PROVIDERS = ['chatgpt', 'claude', 'antigravity'];
function defaults(prompt = '') {
  return { automationEnabled: false, intervalHours: 1, mode: 'draft', keyword: '',
    prompts: [{ id: 'default', name: '기본 글쓰기', text: prompt }], selectedPromptId: 'default',
    stages: ['chatgpt', 'claude', 'antigravity', 'chatgpt'].map((provider, i) => ({ provider, role: ROLES[i], model: '' })),
    imageRetryLimit: 2, includeGoogle: true, googleReferenceCount: 4, progressHeight: 170, progressCollapsed: false };
}
function normalizeBlog(input = {}, prompt = '') {
  if (!input || typeof input !== 'object' || Array.isArray(input)) input = {};
  const value = { ...defaults(prompt), ...input };
  value.automationEnabled = input.automationEnabled === true;
  value.intervalHours = Number.isInteger(Number(value.intervalHours)) && Number(value.intervalHours) >= 1 && Number(value.intervalHours) <= 6 ? Number(value.intervalHours) : 1;
  value.mode = ['draft', 'publish', 'local'].includes(value.mode) ? value.mode : 'draft';
  value.keyword = String(value.keyword || '').normalize('NFC').trim().slice(0, 160);
  value.prompts = Array.isArray(value.prompts) ? value.prompts.filter(p => p && typeof p.id === 'string' && typeof p.name === 'string' && typeof p.text === 'string')
    .slice(0, 30).map(p => ({ id: p.id.slice(0, 100), name: p.name.slice(0, 100), text: p.text.slice(0, 40000) })) : [];
  value.prompts = value.prompts.filter((p, i, all) => p.id && p.name && all.findIndex(v => v.id === p.id) === i);
  if (!value.prompts.length) value.prompts = defaults(prompt).prompts;
  if (!value.prompts.some(p => p.id === value.selectedPromptId)) value.selectedPromptId = value.prompts[0].id;
  value.stages = Array.isArray(value.stages) ? value.stages.slice(0, 4).map((s, i) => ({
    provider: PROVIDERS.includes(s?.provider) ? s.provider : 'chatgpt',
    role: ROLES.includes(s?.role) ? s.role : ROLES[i], model: String(s?.model || '').trim().slice(0, 120)
  })) : defaults(prompt).stages;
  if (!value.stages.length) value.stages = defaults(prompt).stages.slice(0, 1);
  value.imageRetryLimit = [0, 1, 2].includes(Number(value.imageRetryLimit)) ? Number(value.imageRetryLimit) : 2;
  value.includeGoogle = value.includeGoogle !== false;
  value.googleReferenceCount = Math.round(Math.max(1, Math.min(10, Number(value.googleReferenceCount) || 4)));
  value.progressHeight = Math.round(Math.max(60, Math.min(600, Number(value.progressHeight) || 170)));
  value.progressCollapsed = value.progressCollapsed === true;
  return value;
}
function atomicJson(file, data) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const temporary = `${file}.${crypto.randomUUID()}.tmp`;
  try {
    fs.writeFileSync(temporary, JSON.stringify(data, null, 2), { encoding: 'utf8', mode: 0o600 });
    fs.renameSync(temporary, file);
  } finally { if (fs.existsSync(temporary)) fs.unlinkSync(temporary); }
}
class SettingsStore {
  constructor(directory, prompt) { this.file = path.join(directory, 'settings.json'); this.prompt = prompt; }
  get() {
    let saved = {}, recovered = false;
    const read = file => {
      const value = JSON.parse(fs.readFileSync(file, 'utf8').replace(/^\uFEFF/, ''));
      if (!value || typeof value !== 'object' || Array.isArray(value)
          || (value.blog !== undefined && (!value.blog || typeof value.blog !== 'object' || Array.isArray(value.blog)))) {
        throw new Error('Invalid settings object');
      }
      return value;
    };
    if (fs.existsSync(this.file) || fs.existsSync(`${this.file}.last-good`)) {
      try { saved = read(this.file); }
      catch {
        try { saved = read(`${this.file}.last-good`); recovered = true; }
        catch { throw new Error('설정 파일을 읽지 못했습니다. 기존 파일을 보존했으니 실행 자료 폴더의 settings.json을 확인하세요.'); }
      }
    }
    const blog = normalizeBlog(saved.blog, this.prompt);
    // A backup may predate the user's Stop click. Recovery must never silently
    // re-enable publication while preserving all writing preferences.
    if (recovered) blog.automationEnabled = false;
    return { ...saved, blog };
  }
  set(patch, { automation = false } = {}) {
    if (!patch || typeof patch !== 'object' || Array.isArray(patch)
        || (patch.blog !== undefined && (!patch.blog || typeof patch.blog !== 'object' || Array.isArray(patch.blog)))) {
      throw new Error('저장할 설정 형식이 올바르지 않습니다.');
    }
    const current = this.get();
    const blog = { ...current.blog, ...(patch.blog || {}) };
    // A delayed text autosave cannot re-enable automation after the user stopped it.
    if (!automation) blog.automationEnabled = current.blog.automationEnabled;
    const next = { ...current, ...patch, blog: normalizeBlog(blog, this.prompt) };
    atomicJson(`${this.file}.last-good`, current);
    atomicJson(this.file, next);
    return next;
  }
}
function safeLog(value) {
  return String(value || '').replace(/\bBearer\s+[^\s]+/gi, 'Bearer [가림]')
    .replace(/((?:access_token|refresh_token|api_key|password|NID_AUT|NID_SES)\s*[=:]\s*)[^\s,;]+/gi, '$1[가림]').slice(0, 4000);
}
class BackendRunner {
  constructor({ executable, args = [], dataDir, emit = () => {}, spawnProcess = spawn,
    timers = { setTimeout, clearTimeout }, platform = process.platform, killProcess = process.kill.bind(process) }) {
    Object.assign(this, { executable, args, dataDir, emit, spawnProcess, timers, platform, killProcess }); this.work = null;
  }
  invoke(command, payload = {}, { work = false } = {}) {
    if (work && this.work) return Promise.reject(new Error('이미 글 작성 작업이 실행 중입니다.'));
    return new Promise((resolve, reject) => {
      const input = JSON.stringify(payload);
      if (Buffer.byteLength(input) > 4 * 1024 * 1024) throw new Error('작성 요청이 제한 크기를 초과했습니다.');
      const child = this.spawnProcess(this.executable, [...this.args, '--data-dir', this.dataDir, command], {
        cwd: this.dataDir, stdio: ['pipe', 'pipe', 'pipe'], shell: false,
        detached: this.platform === 'darwin', env: { ...process.env, PYTHONUNBUFFERED: '1', PYTHONIOENCODING: 'utf-8' }
      });
      if (work) this.work = child;
      const stdoutDecoder = new StringDecoder('utf8'), stderrDecoder = new StringDecoder('utf8');
      let partial = '', result, failure, stderr = '', settled = false, bytes = 0;
      const finish = error => {
        if (settled) return; settled = true;
        this.timers.clearTimeout(timeout);
        if (error || failure) reject(error || failure); else if (result !== undefined) resolve(result);
        else reject(new Error(`작성 엔진이 결과 없이 종료됐습니다. ${safeLog(stderr)}`));
      };
      const failAndStop = error => { this.terminate(child); finish(error); };
      const timeout = this.timers.setTimeout(() => failAndStop(new Error('작성 엔진 제한 시간을 초과했습니다. 저장된 회차에서 이어서 실행할 수 있습니다.')), work ? 7200000 : 90000);
      const lineEvent = line => {
        let event;
        try { event = JSON.parse(line); } catch { return; }
        if (!event || typeof event !== 'object') return;
        if (event.event === 'result') {
          if (result !== undefined) { failAndStop(new Error('작성 엔진이 중복 완료 결과를 반환했습니다.')); return; }
          result = event.result;
        } else if (event.event === 'error') {
          failure = Object.assign(new Error(safeLog(event.message)), { code: String(event.code || '').slice(0, 100) });
        } else if (event.event === 'progress') this.emit({ message: safeLog(event.message), stage: String(event.stage || '').slice(0, 100) });
      };
      child.stdout.on('data', data => {
        if (settled) return;
        const chunk = Buffer.isBuffer(data) ? data : Buffer.from(data);
        bytes += chunk.length;
        partial += stdoutDecoder.write(chunk);
        if (partial.length > 8 * 1024 * 1024 || bytes > 32 * 1024 * 1024) {
          failAndStop(new Error('작성 엔진 응답이 제한 크기를 초과했습니다.')); return;
        }
        let index;
        while ((index = partial.indexOf('\n')) >= 0) {
          const line = partial.slice(0, index); partial = partial.slice(index + 1);
          lineEvent(line);
          if (settled) return;
        }
      });
      child.stderr.on('data', data => { stderr = (stderr + stderrDecoder.write(Buffer.isBuffer(data) ? data : Buffer.from(data))).slice(-5000); });
      child.on('error', error => finish(new Error(`작성 엔진 실행 실패: ${safeLog(error.message)}`)));
      child.on('close', (code, signal) => {
        this.timers.clearTimeout(child.stopTimer);
        if (this.work === child) this.work = null;
        if (!settled) {
          partial += stdoutDecoder.end(); stderr = (stderr + stderrDecoder.end()).slice(-5000);
          if (partial.trim()) lineEvent(partial);
          finish(code !== 0 && !failure ? new Error(`작성 엔진 종료(${signal || code}): ${safeLog(stderr)}`) : null);
        }
      });
      child.stdin.on('error', () => {});
      try { child.stdin.end(input); } catch (error) { failAndStop(new Error(`작성 요청 전달 실패: ${safeLog(error.message)}`)); }
    });
  }
  terminate(child = this.work) {
    if (!child || child.exitCode !== null || child.stopTimer) return;
    try { child.kill('SIGTERM'); } catch {}
    child.stopTimer = this.timers.setTimeout(() => {
      try { if (this.platform === 'darwin') this.killProcess(-child.pid, 'SIGKILL'); else child.kill('SIGKILL'); } catch {}
    }, 10000);
    child.stopTimer.unref?.();
  }
}
class BlogController {
  constructor({ store, backend, collect, ensureBrowser, emit, now = Date.now, timers = { setTimeout, clearTimeout } }) {
    Object.assign(this, { store, backend, collect, ensureBrowser, emit, now, timers });
    this.busy = false; this.nextRunAt = null; this.lastResult = null; this.logs = []; this.timer = null; this.stopping = false;
    this.status = 'idle'; this.lastError = null; this.scheduleGeneration = 0; this.scheduledAt = null;
  }
  state() { return { busy: this.busy, status: this.status, lastError: this.lastError, automationEnabled: this.store.get().blog.automationEnabled, nextRunAt: this.nextRunAt, lastResult: this.lastResult, logs: [...this.logs] }; }
  progress(value) {
    if (value.status) this.status = value.status;
    if (value.message) {
      const message = `${new Date(this.now()).toLocaleTimeString('ko-KR')} ${safeLog(value.message)}`;
      this.logs.push(message); if (this.logs.length > 5000) this.logs.splice(0, this.logs.length - 5000);
    }
    this.emit({ ...this.state(), ...value });
  }
  initialize() { if (this.store.get().blog.automationEnabled && !this.busy && this.timer === null) this.start({ automatic: true }); }
  setAutomation(enabled) {
    if (typeof enabled !== 'boolean') throw new Error('자동화 설정은 켜기 또는 끄기여야 합니다.');
    this.store.set({ blog: { automationEnabled: enabled } }, { automation: true });
    if (!enabled) this.stop(); else if (!this.busy && this.timer === null) this.start({ automatic: true });
    this.progress({ message: enabled ? '자동화 켜짐 · 저장한 간격과 완료 동작을 사용합니다.' : '자동화 꺼짐 · 수동 작성 버튼을 사용할 수 있습니다.' });
    return this.state();
  }
  settingsChanged() { if (this.timer !== null && !this.busy) this.schedule(false); }
  start({ keyword = '', mode, automatic = false } = {}) {
    if (this.busy) throw new Error('이미 글 작성 중입니다. 현재 작업이 끝난 뒤 실행하세요.');
    keyword = String(keyword || '').normalize('NFC').trim();
    if (!automatic && (!keyword || keyword.length > 160)) throw new Error('글을 작성할 키워드를 1~160자로 입력하세요.');
    if (mode && !['draft', 'publish', 'local'].includes(mode)) throw new Error('완료 동작이 올바르지 않습니다.');
    const saved = this.store.get();
    const snapshot = JSON.parse(JSON.stringify({ ...saved, blog: { ...saved.blog, ...(mode ? { mode } : {}) } }));
    this.timers.clearTimeout(this.timer); this.timer = null; this.nextRunAt = null; this.busy = true; this.stopping = false;
    this.scheduleGeneration++; this.lastResult = null; this.lastError = null;
    this.progress({ status: 'running', message: automatic ? '자동 회차 시작 · 연관어와 최신 검색 의도를 확인합니다.' : `'${keyword}' 연관어와 사람들이 궁금해하는 내용을 확인합니다.`, stage: 'research' });
    this.running = this.run(snapshot, keyword, automatic).catch(error => {
      this.lastError = this.stopping ? null : safeLog(error.message);
      this.progress({ status: this.stopping ? 'stopped' : 'error', message: this.stopping ? '작업을 중지했습니다. 저장된 회차는 유지합니다.' : error.message });
    }).finally(() => {
      const stopped = this.stopping;
      this.busy = false; this.stopping = false; this.schedule();
      this.progress({ status: stopped ? 'stopped' : this.status });
    });
    return this.state();
  }
  async run(settings, keyword, automatic) {
    const research = await this.collect(keyword, automatic);
    if (this.stopping) return;
    // Browser authentication is checked by the engine before paid generation.
    if (settings.blog.mode !== 'local' || settings.blog.includeGoogle) {
      try { await this.ensureBrowser(); }
      catch (error) { if (settings.blog.mode !== 'local') throw error; this.progress({ message: `참고사진 검색 생략: ${error.message}` }); }
    }
    if (this.stopping) return;
    this.lastResult = await this.backend.invoke('run', { ...research, settings, keyword, automatic }, { work: true });
    if (!this.lastResult || typeof this.lastResult !== 'object') throw new Error('작성 엔진의 완료 결과 형식이 올바르지 않습니다.');
    if (this.stopping) return;
    this.progress({ status: 'complete', lastResult: this.lastResult, message: this.lastResult.message || '글 작성 작업을 완료했습니다.' });
  }
  schedule(reset = true) {
    this.timers.clearTimeout(this.timer); this.timer = null; this.nextRunAt = null;
    const generation = ++this.scheduleGeneration;
    const blog = this.store.get().blog;
    if (!blog.automationEnabled || this.busy) { this.scheduledAt = null; return; }
    if (reset || this.scheduledAt === null) this.scheduledAt = this.now();
    const deadline = this.scheduledAt + blog.intervalHours * 3600000;
    this.nextRunAt = new Date(deadline).toISOString();
    this.timer = this.timers.setTimeout(() => {
      if (generation !== this.scheduleGeneration || this.busy || !this.store.get().blog.automationEnabled) return;
      this.start({ automatic: true });
    }, Math.max(0, deadline - this.now()));
  }
  stop() {
    this.store.set({ blog: { automationEnabled: false } }, { automation: true });
    this.timers.clearTimeout(this.timer); this.timer = null; this.nextRunAt = null; this.stopping = this.busy;
    this.scheduleGeneration++; this.scheduledAt = null;
    this.backend.terminate();
    this.progress({ status: this.busy ? 'stopping' : 'stopped', message: '자동화를 중지합니다. 이미 제출한 글은 다시 발행하지 않습니다.' });
    return this.state();
  }
}
module.exports = { defaults, normalizeBlog, atomicJson, SettingsStore, BackendRunner, BlogController, safeLog };
