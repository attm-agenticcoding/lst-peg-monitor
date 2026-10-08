#!/usr/bin/env node
'use strict';

// Cheap wake-up only. The receiver owns the durable UTC-slot/lease CAS and all
// source reads. No manual request IDs, credentials or source budgets are made here.
const { spawnSync } = require('node:child_process');
const REPOSITORY = 'attm-agenticcoding/lst-peg-monitor';
const WORKFLOW = 'lido-manual.yml';
const SNAPSHOT = 'data/lido-cutoff-snapshot.json';
const ACTIVE = ['queued', 'in_progress', 'waiting', 'requested', 'pending'];

function github(path, payload) {
  const args = ['api', `repos/${REPOSITORY}${path}`];
  if (payload) args.push('--method', 'POST', '--input', '-');
  // Fixed destination, finite single request, no secrets in argv or error logs.
  const result = spawnSync('gh', args, {
    input: payload ? JSON.stringify(payload) : undefined,
    encoding: 'utf8', timeout: 10000, maxBuffer: 8 * 1024 * 1024,
  });
  if (result.error || result.status !== 0) throw new Error('GitHub request failed; no retry in this relay process this slot');
  return result.stdout.trim() ? JSON.parse(result.stdout) : null;
}

function readRefresh(api) {
  const file = api(`/contents/${SNAPSHOT}?ref=main`);
  if (file?.encoding !== 'base64' || typeof file.content !== 'string') throw new Error('Invalid snapshot response');
  const refresh = JSON.parse(Buffer.from(file.content, 'base64').toString('utf8')).refresh;
  if (!refresh || typeof refresh !== 'object' || Array.isArray(refresh)) throw new Error('Missing refresh state');
  return refresh;
}

function slotKey(now) {
  return new Date(Math.floor(now / 43200000) * 43200000).toISOString().replace('.000Z', 'Z');
}

function validUtc(value) {
  return typeof value === 'string' && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/.test(value)
    && Number.isFinite(Date.parse(value)) && new Date(value).toISOString().replace('.000Z', 'Z') === value;
}

function slotAttempted(refresh, now) {
  const slot = slotKey(now);
  if (refresh.attemptSlot != null) {
    if (!validUtc(refresh.attemptSlot) || slotKey(Date.parse(refresh.attemptSlot)) !== refresh.attemptSlot) {
      throw new Error('Invalid durable attempt slot');
    }
    return refresh.attemptSlot >= slot;
  }
  const legacy = refresh.attemptDay;
  if (legacy == null) return false;
  if (typeof legacy !== 'string' || !validUtc(legacy + 'T00:00:00Z')) throw new Error('Invalid legacy attempt day');
  const day = slot.slice(0, 10);
  if (legacy !== day) return legacy > day;
  if (slot.slice(11, 13) === '12' && ['scheduled', 'relay-dispatch'].includes(refresh.trigger)
      && validUtc(refresh.attemptAt)) {
    return refresh.attemptAt.slice(0, 10) !== legacy || slotKey(Date.parse(refresh.attemptAt)) >= slot;
  }
  return true; // Ambiguous or manual-overwritten legacy state consumes this day.
}

function stopReason(refresh, now) {
  if (refresh.accessBlocked === true) return { status: 'done', reason: 'source-access-blocked' };
  if (slotAttempted(refresh, now)) return { status: 'done', reason: 'UTC-slot-already-attempted' };
  if (refresh.lease != null) {
    if (!Number.isSafeInteger(refresh.lease.expiresAtEpoch)) throw new Error('Invalid refresh lease');
    if (refresh.lease.expiresAtEpoch > Math.floor(now / 1000)) return { status: 'skipped', reason: 'active-lease' };
  }
  return null;
}

function checkDaily({ api = github, now = Date.now, env = process.env } = {}) {
  if (env.GITHUB_ACTIONS !== 'true' || env.GITHUB_REPOSITORY !== REPOSITORY
      || env.GITHUB_REF !== 'refs/heads/main' || !/^[1-9][0-9]*$/.test(env.GITHUB_RUN_ID || '')) {
    throw new Error('Daily fallback requires this repository main relay');
  }
  const started = now();
  const slot = slotKey(started);
  let stopped = stopReason(readRefresh(api), started);
  if (stopped) return stopped;
  for (const status of ACTIVE) {
    const runs = api(`/actions/workflows/${WORKFLOW}/runs?status=${status}&per_page=1`);
    if (!Number.isSafeInteger(runs?.total_count) || runs.total_count < 0) throw new Error('Invalid workflow run response');
    if (runs.total_count > 0) return { status: 'skipped', reason: 'active-Lido-run' };
  }
  // Re-read shared state immediately before dispatch. A later race is closed by
  // the receiver's exact-head lease CAS, before any expensive source query.
  const refresh = readRefresh(api);
  const beforeSend = now();
  if (slotKey(beforeSend) !== slot) return { status: 'skipped', reason: 'UTC-slot-changed' };
  stopped = stopReason(refresh, beforeSend);
  if (stopped) return stopped;
  api(`/actions/workflows/${WORKFLOW}/dispatches`, {
    ref: 'main', inputs: { mode: 'scheduled', attempt_slot: slot, relay_run_id: env.GITHUB_RUN_ID },
  });
  return { status: 'dispatched', attemptSlot: slot, relayRunId: env.GITHUB_RUN_ID };
}

if (require.main === module) {
  try {
    const result = checkDaily();
    console.log(JSON.stringify({ step: 'Lido-daily-fallback', ...result }));
    // Tell the shell to stop checking this slot after a consumed slot or dispatch.
    process.exitCode = result.status === 'skipped' ? 0 : 10;
  } catch (_) {
    console.error('::warning::Lido daily fallback stopped after an API/input failure; no further checks in this relay process this slot');
    process.exitCode = 1;
  }
}

module.exports = { checkDaily, ACTIVE, slotKey, slotAttempted };
