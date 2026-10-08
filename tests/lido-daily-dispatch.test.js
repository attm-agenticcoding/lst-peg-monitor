'use strict';
const assert = require('node:assert/strict');
const { checkDaily, ACTIVE } = require('../scripts/lido-daily-dispatch');
const env = { GITHUB_ACTIONS: 'true', GITHUB_REPOSITORY: 'attm-agenticcoding/lst-peg-monitor',
  GITHUB_REF: 'refs/heads/main', GITHUB_RUN_ID: '123456' };
const at = Date.parse('2026-10-08T01:00:00Z');
const day = '2026-10-08';

function fixture(refresh = {}, active = null, secondRefresh = refresh) {
  const calls = [];
  let reads = 0;
  return { calls, api(path, payload) {
    calls.push({ path, payload });
    if (path.startsWith('/contents/')) {
      const value = reads++ ? secondRefresh : refresh;
      return { encoding: 'base64', content: Buffer.from(JSON.stringify({ refresh: value })).toString('base64') };
    }
    if (path.includes('/runs?')) return { total_count: path.includes(`status=${active}&`) ? 1 : 0 };
    assert.equal(path, '/actions/workflows/lido-manual.yml/dispatches');
    return null;
  } };
}
function run(f, now = () => at) { return checkDaily({ api: f.api, now, env }); }
function unsent(f) { assert.equal(f.calls.filter(c => c.payload).length, 0); }

const send = fixture({ attemptDay: '2026-10-07', lease: null });
assert.equal(run(send).status, 'dispatched');
assert.deepEqual(send.calls.at(-1).payload, {
  ref: 'main', inputs: { mode: 'daily', attempt_day: day, relay_run_id: '123456' },
});
assert.equal(send.calls.filter(c => c.payload).length, 1);
for (const refresh of [{ attemptDay: day }, { accessBlocked: true },
  { lease: { expiresAtEpoch: at / 1000 + 2100 } }]) {
  const f = fixture(refresh);
  assert.notEqual(run(f).status, 'dispatched');
  assert.equal(f.calls.length, 1);
  unsent(f);
}
for (const status of ACTIVE) {
  const f = fixture({}, status);
  assert.equal(run(f).reason, 'active-Lido-run');
  unsent(f);
}
for (const changed of [{ attemptDay: day }, { lease: { expiresAtEpoch: at / 1000 + 2100 } }, { accessBlocked: true }]) {
  const f = fixture({}, null, changed);
  assert.notEqual(run(f).status, 'dispatched');
  unsent(f);
}
const midnight = fixture();
let clockCalls = 0;
assert.equal(run(midnight, () => clockCalls++ ? at + 86400000 : at).reason, 'UTC-day-changed');
unsent(midnight);
assert.equal(run(fixture({ lease: { expiresAtEpoch: at / 1000 } })).status, 'dispatched');
for (const refresh of [{ lease: {} }, null, []]) assert.throws(() => run(fixture(refresh)));
let attempts = 0;
assert.throws(() => checkDaily({ env, now: () => at, api() { attempts++; throw new Error('denied'); } }));
assert.equal(attempts, 1);
const uncertain = fixture();
assert.throws(() => checkDaily({ env, now: () => at, api(path, payload) {
  const result = uncertain.api(path, payload);
  if (payload) throw new Error('unknown dispatch outcome');
  return result;
} }));
assert.equal(uncertain.calls.filter(c => c.payload).length, 1);
for (const changed of [{ GITHUB_ACTIONS: 'false' }, { GITHUB_REPOSITORY: 'other/repo' },
  { GITHUB_REF: 'refs/heads/other' }, { GITHUB_RUN_ID: '../bad' }]) {
  const f = fixture();
  assert.throws(() => checkDaily({ env: { ...env, ...changed }, api: f.api }));
  assert.equal(f.calls.length, 0);
}
console.log('Lido daily dispatch: preflight, active runs, day/lease races, input and failure tests passed');
