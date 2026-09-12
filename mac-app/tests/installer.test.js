const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const installer = fs.readFileSync(path.join(__dirname, '..', 'Install-Blog.command'), 'utf8');
const jxa = installer.match(/<<'JXA'\r?\n([\s\S]*?)\r?\nJXA/)[1];
const identifier = 'com.dicacros.picturecleaner.mac';
const target = '/Users/test/Applications/Blog.app';
const legacy = '/Applications/Picture Cleaner.app';

function application(appPath, { id = identifier, acceptsQuit = true, exits = true } = {}) {
  return {
    bundleIdentifier: { js: id }, bundleURL: { path: { stringByStandardizingPath: { js: appPath } } },
    quitRequests: 0, isTerminated: false,
    get terminate() { this.quitRequests++; if (exits && acceptsQuit) this.isTerminated = true; return acceptsQuit; }
  };
}

function run(applications) {
  let now = 0;
  const context = vm.createContext({
    ObjC: { import() {} }, Date: { now: () => now },
    $: {
      NSWorkspace: { sharedWorkspace: { runningApplications: { js: applications } } },
      NSString: { stringWithString: value => ({ stringByStandardizingPath: { js: path.posix.normalize(value) } }) },
      NSThread: { sleepForTimeInterval: seconds => { now += seconds * 1000; } }
    }
  });
  vm.runInContext(jxa, context);
  context.run([identifier, target, legacy]);
  return now;
}

test('first install with no running Blog requires no application launch or quit', () => {
  assert.equal(run([]), 0);
});

test('installer quits only matching bundle IDs at its allowed app locations', () => {
  const own = application(target), old = application(legacy);
  const namesake = application(target, { id: 'other.app' });
  const unrelatedLocation = application('/Users/test/Other/Blog.app');
  run([own, old, namesake, unrelatedLocation]);
  assert.equal(own.quitRequests, 1);
  assert.equal(old.quitRequests, 1);
  assert.equal(namesake.quitRequests, 0);
  assert.equal(unrelatedLocation.quitRequests, 0);
});

test('installer aborts when a running Blog refuses a graceful quit', () => {
  const own = application(target, { acceptsQuit: false });
  assert.throws(() => run([own]), /종료할 수 없습니다/);
  assert.equal(own.quitRequests, 1);
  assert.equal(own.isTerminated, false);
});

test('installer has a bounded wait when Blog accepts quit but remains running', () => {
  const own = application(target, { exits: false });
  assert.throws(() => run([own]), /기다리다 설치를 중단/);
  assert.equal(own.quitRequests, 1);
  assert.equal(own.isTerminated, false);
});
