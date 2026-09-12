const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.join(__dirname, '..');

test('preload exposes both ordinary and device-code login through the same restricted IPC', async () => {
  let api;
  const calls = [];
  vm.runInNewContext(fs.readFileSync(path.join(root, 'preload.js'), 'utf8'), {
    require: () => ({ contextBridge: { exposeInMainWorld: (_name, value) => { api = value; } },
      ipcRenderer: { invoke: (...args) => calls.push(args) } })
  });
  await api.loginCli('claude');
  await api.loginCli('chatgpt', { deviceAuth: true });
  assert.deepEqual(JSON.parse(JSON.stringify(calls)), [
    ['blog-cli-login', 'claude', {}], ['blog-cli-login', 'chatgpt', { deviceAuth: true }]
  ]);
});

function rendererFixture(loginCli) {
  const accounts = { children: [], replaceChildren(...children) { this.children = children; } };
  const errors = [], progress = [], loggingIn = new Set();
  const source = fs.readFileSync(path.join(root, 'renderer', 'renderer.js'), 'utf8');
  const render = source.slice(source.indexOf('function renderCliAccounts('), source.indexOf('async function checkCliAccounts('));
  const context = {
    PROVIDERS: { chatgpt: 'ChatGPT', claude: 'Claude', antigravity: 'Antigravity' }, cliLoggingIn: loggingIn,
    document: { createElement: tag => ({ tag, dataset: {}, children: [], append(...children) { this.children.push(...children); } }) },
    $: () => accounts, window: { picture: { loginCli } },
    appendProgress: message => progress.push(message), showBlogError: error => errors.push(error),
    updateBlogButtons: () => {
      for (const account of accounts.children) for (const button of account.children.filter(node => node.tag === 'button')) {
        button.disabled = loggingIn.has(button.dataset.provider);
      }
    }
  };
  vm.runInNewContext(`${render}\nrenderCliAccounts();`, context);
  return { accounts, errors, progress, loggingIn };
}

test('only ChatGPT offers device-code login, with all ordinary login choices retained', async () => {
  const calls = [];
  const f = rendererFixture(async (...args) => { calls.push(args); return { message: '로그인 완료 후 재확인' }; });
  const buttons = f.accounts.children.map(account => account.children.filter(node => node.tag === 'button'));
  assert.deepEqual(buttons.map(items => items.map(item => item.textContent)), [['로그인', '기기 코드 로그인'], ['로그인'], ['로그인']]);
  await buttons[0][1].onclick();
  await buttons[1][0].onclick();
  assert.deepEqual(JSON.parse(JSON.stringify(calls)), [['chatgpt', { deviceAuth: true }], ['claude', { deviceAuth: false }]]);
  assert.equal(f.progress.length, 2);
  assert.equal(f.errors.length, 0);
});

test('device-code and ordinary login cannot launch concurrently for the same account', async () => {
  let reject, count = 0;
  const f = rendererFixture(() => { count++; return new Promise((_resolve, failure) => { reject = failure; }); });
  const buttons = f.accounts.children[0].children.filter(node => node.tag === 'button');
  const pending = buttons[1].onclick();
  assert.ok(buttons.every(button => button.disabled));
  await buttons[0].onclick();
  assert.equal(count, 1);
  reject(new Error('로그인 창 실행 실패'));
  await pending;
  assert.equal(f.errors.length, 1);
  assert.equal(f.progress.length, 0);
  assert.ok(buttons.every(button => !button.disabled));
});
