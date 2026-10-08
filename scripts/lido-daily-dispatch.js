#!/usr/bin/env node
'use strict';

// Cheap wake-up only. The receiver owns the durable UTC-day/lease CAS and all
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
  if (result.error || result.status !== 0) throw new Error('GitHub request failed; no retry in this relay process today');
  return result.stdout.trim() ? JSON.parse(result.stdout) : null;
}

function readRefresh(api) {
  const file = api(`/contents/${SNAPSHOT}?ref=main`);
  if (file?.encoding !== 'base64' || typeof file.content !== 'string') throw new Error('Invalid snapshot response');
  const refresh = JSON.parse(Buffer.from(file.content, 'base64').toString('utf8')).refresh;
  if (!refresh || typeof refresh !== 'object' || Array.isArray(refresh)) throw new Error('Missing refresh state');
  return refresh;
}

function stopReason(refresh, day, now) {
  if (refresh.accessBlocked === true) return { status: 'done', reason: 'source-access-blocked' };
  if (refresh.attemptDay === day) return { status: 'done', reason: 'UTC-day-already-attempted' };
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
  const day = new Date(started).toISOString().slice(0, 10);
  let stopped = stopReason(readRefresh(api), day, started);
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
  if (new Date(beforeSend).toISOString().slice(0, 10) !== day) return { status: 'skipped', reason: 'UTC-day-changed' };
  stopped = stopReason(refresh, day, beforeSend);
  if (stopped) return stopped;
  api(`/actions/workflows/${WORKFLOW}/dispatches`, {
    ref: 'main', inputs: { mode: 'daily', attempt_day: day, relay_run_id: env.GITHUB_RUN_ID },
  });
  return { status: 'dispatched', attemptDay: day, relayRunId: env.GITHUB_RUN_ID };
}

if (require.main === module) {
  try {
    const result = checkDaily();
    console.log(JSON.stringify({ step: 'Lido-daily-fallback', ...result }));
    // Tell the shell to stop checking this day after a consumed day or dispatch.
    process.exitCode = result.status === 'skipped' ? 0 : 10;
  } catch (_) {
    console.error('::warning::Lido daily fallback stopped after an API/input failure; no further checks in this relay process today');
    process.exitCode = 1;
  }
}

module.exports = { checkDaily, ACTIVE };
