# Daily Lido mechanism refresh

The authorized schedule is one attempt each day at **00:00 UTC**. An explicitly
requested one-off run uses the same collector and publication safeguards, with
its own idempotency key. The schedule is managed outside this repository. This
change creates no cron, workflow, relay, account, credentials or paid service;
the existing spot-price sampler remains independent.

## What is recomputed

Each successful run downloads a fresh public finalized Fulu BeaconState,
recomputes its complete SSZ root, and authenticates its execution block using
the immediate child execution block's EIP-4788 commitment and finalized head.
It advances `data/lido-collector-state.json` using complete bounded event ranges,
then reads balances at that exact execution block. The retained checkpoint is
sufficient after restart; no private scratch directory or old raw SSZ is needed.

The execution source is the public, account-free MEV Blocker endpoint
`https://rpc.mevblocker.io`, documented by the provider at
<https://docs.mevblocker.io/how-to/integrate/Wallets>. The collector uses only the
three approved read methods below; it does not submit transactions. There is no
automatic provider fallback, redirect following or API key. Acquisition responses
may be retained only inside the same authorized run and fixed target, under the
recovery rules below. A different run always starts with a newly downloaded
BeaconState and does not import another run's responses. Switching this source
requires explicit approval and source validation.

The integer consensus model produces known-state cash arrivals at 14 upcoming
reference reports for recurring-legacy and eight-reserved-slot stress scenarios.
The integer FIFO engine then recomputes six independent hypothetical joins of
100, 200, 300, 500, 1000 and 1500 stETH. The 1500 tier splits into 1000 + 500 and
finishes only when its last request finishes. Uncovered completion remains null.

These remain conditional nominal scenarios, with `live=false`,
`calibrated=false`, and non-actionable UI results. Reference dates are not
publication dates, claim dates, calibrated confidence bounds or guarantees.
Future cash is assumed report-admissible; unknown future rewards, penalties,
exits and missed blocks are not forecast. Historical unpinned configuration
provenance stays explicitly conditional. Unsupported changes, paused/Bunker
state, unsupported consensus state or incomplete inputs stop new publication.

## Three distinct clocks

- `asOf`: authenticated chain state time, never replaced with a run timestamp
- `attemptAt`, `finishedAt`, `lastSuccessAt`: operational run timestamps
- Daily schedule: `mode=daily`, `expectedIntervalSeconds=86400`,
  `scheduleUtc=00:00`

Every newly acquired result must be at most **5400 seconds (90 minutes)** old
at acquisition, completion and publication. The daily display's operational
age allowance is separately `maxOperationalAgeSeconds=91800`, one scheduled
interval plus that source allowance. It does not permit publishing a day-old
source. Economic quote compatibility is still only 300 seconds, with the exact
queue block/time and actual purchased stETH amount required. A daily model is
not a live economic quote.

Failures retain the previous successful source time, scenarios and checkpoint;
only refresh status changes. Explicit source-access denials must stop acquisition
until resolved, including manual attempts. Do not substitute a denied provider
or method. The approved execution methods are only `eth_getLogs`,
`eth_getBlockByNumber` and `eth_getBalance`; no `eth_call` is added.

A reviewable transport interruption can preserve this run's acquisition progress
while all new reads stop. It does not produce a complete snapshot or advance the
durable checkpoint. A source HTTP/RPC 401 or 403, a refused redirect, invalid data
or unsupported protocol state remains terminal; it is not a recoverable source
retry. An unknown tunnel rejection does not establish that the origin provider
denied access, but it is also not permission to continue. Recovery requires the
normal supported review of the same source and action.

## Scheduled or explicit one-off lease

Use a fresh checkout of the exact current `main` commit. Preserve the snapshot
and collector-state blob SHAs. Requirements are Python 3.12+, a C++17 compiler,
about 1 GB memory and temporary disk, and already authorized public reads and
repository publication. Downloaded SSZ is about 337 MB, bounded at 450 MiB.

Scheduled invocation:

```sh
python scripts/lido/refresh.py prepare-lease --output /tmp/lido-lease.json
```

Explicit user-requested one-off invocation:

```sh
python scripts/lido/refresh.py prepare-lease --trigger manual \
  --manual-request-id manual-20261005-request1 --output /tmp/lido-lease.json
```

Ordinary scheduled and manual leases use 256 logical RPC attempts. Only for an
explicitly approved one-off catch-up, add `--rpc-call-budget 320` to the manual
`prepare-lease` command. Read back `trigger=manual`, the exact `requestKey` and
`runId`, and `rpcCallBudget=320` in the published lease before starting. Scheduled
leases cannot select 320. `run`, `resume`, `abort` and `once` cannot override the
budget; the journal fixes it to the original lease and counts failed and unknown
attempts too. Completion releases that lease, and the next ordinary lease again
defaults to 256. A larger budget is not authorization for a new request or retry.
The 25-minute calculation, 35-minute lease, source freshness and all validation
requirements remain unchanged.

The manual ID identifies the user's specific requested run. Choose a stable,
non-sensitive ID and reuse it on delivery/retry of that same request; a random
new ID on every retry defeats deduplication. The last 128 manual IDs are
retained. An old ID outside that bounded history must not be replayed by the
caller. A new manual ID requires a new explicit user request, not an automatic
retry. Manual runs never clear or change the scheduled `attemptDay` key.

Scheduled attempts deduplicate on UTC `attemptDay`. Legacy `attemptHour` may
remain as historical metadata, but is ignored by daily scheduling. A failed
scheduled attempt consumes that day's attempt key. A new day is eligible.
Manual attempts are independent of that key, but every trigger shares the
same active lease, so overlapping scheduled/manual runs cannot both acquire
ownership. If a scheduled wake encounters an active lease, it may retry lease
acquisition once that lease is released, subject to the same daily key.

`stage=skipped` means no new run may begin. Otherwise, publish only the proposed
snapshot lease using a content-SHA compare-and-swap, read it back, verify its
run ID, and materialize that exact snapshot with the unchanged checkpoint.
Do not overwrite a competing lease or blindly retry an uncertain write.

## Collect and publish

Both trigger types then use exactly the same leased run command. Trigger
arguments belong only to `prepare-lease`; `run` inherits the durable lease:

```sh
python scripts/lido/refresh.py run --run-id RUN_ID --head LEASE_COMMIT_SHA \
  --output /tmp/lido-result.json --workdir /tmp/lido-work-RUN_ID
```

Lease lifetime is 35 minutes and calculation hard limit is 25 minutes. A local
`once` command is verification-only and **cannot be published**, even when its
calculation succeeds. Do not relabel its manifest or remove that restriction.

Before publication, fetch the latest `main` commit and both data files pinned
to that exact commit. Validate ownership, unexpired lease, both expected blob
SHAs, source freshness, trigger/idempotency metadata and scenario shape:

```sh
python scripts/lido/publication.py --manifest /tmp/lido-result.json \
  --current-root FRESH_CHECKOUT --head FRESH_MAIN_SHA \
  --output /tmp/lido-publication.json
```

Success permits exactly two files in one atomic child commit: snapshot and
collector state. Update the ref non-forcibly. A failure permits only the old
snapshot with failure metadata via content-SHA compare-and-swap. A spot-sampler
race can be retried against a newer base only while the expected blobs and lease
still match; do not rerun expensive collection merely because unrelated data
advanced. Verify committed bytes and public Pages JSON before reporting an
update. Keep a blocked publication's small manifest/audit for resolution.

The collector deletes its large SSZ after a successful or terminal result.
Only an eligible paused run retains its original SSZ and acquisition journal.
`--keep-source` is only for explicitly requested local audits. After verified
publication or deliberate discard, remove only that run's temporary files.

## Same-run recovery boundaries

Exit code 2 with `stage=paused` means the collector saved eligible progress,
stopped acquisition and retained the original durable lease. Its manifest has
no publication files. Do not run `prepare-lease` again or publish that manifest.
Check that the old process and its transport callbacks have stopped, obtain the
supported review for the same source/action, and fetch current `main` and both
data files into a separate fresh checkout before resuming:

```sh
python FRESH_CHECKOUT/scripts/lido/refresh.py resume --run-id RUN_ID \
  --workdir /tmp/lido-work-RUN_ID --current-root FRESH_CHECKOUT \
  --head FRESH_MAIN_SHA --attest-current-main \
  --reviewed-same-route 'reference to the actual same-route command review' \
  --output /tmp/lido-result.json
```

The review reference and current-main flag are explicit controller attestations,
not permission tokens. They cannot replace a missing/denied tool review, a real
fresh repository read or the normal network-access controls. Do not change an
endpoint, proxy, request identity or environment to evade a refusal.

If review is denied/unavailable, the original calculation deadline expires, or
the candidate is deliberately discarded, stop the existing worker and finalize
the failure while the original lease is still valid. Fetch current main again
and use the same run/work directory:

```sh
python FRESH_CHECKOUT/scripts/lido/refresh.py abort --run-id RUN_ID \
  --workdir /tmp/lido-work-RUN_ID --current-root FRESH_CHECKOUT \
  --head FRESH_MAIN_SHA --attest-current-main \
  --abort-reason 'actual reason this original run cannot continue' \
  --output /tmp/lido-result.json
```

Run the existing publication gate against another fresh current-main readback
before publishing that failure status and releasing its lease. An expired or
foreign lease never authorizes an overwrite. The 25-minute calculation deadline
leaves a separate ten-minute lease margin for this finalization; do not wait for
the 35-minute lease to expire.

The original run ID, trigger/request key, lease owner and expiry, input blob SHAs,
code identity, SSZ bytes/root, execution block/hash/time and source endpoint are
fixed. Recovery must first use a newly materialized current-main readback to
check the actual durable lease, both input files and the running code. Supplying
the old identity object alone is not a fresh lease or code verification.

The original 25-minute computation deadline keeps running during interruptions,
review waits and process restarts. Recovery never extends the 35-minute lease
or resets the original lease's shared attempt count (normally 256; 320 only for
an explicitly approved manual catch-up). Failed and unknown in-flight attempts
remain charged. The original monotonic clock identity and wall-clock deadline
must still match; a different host/clock or regressed clock is rejected.

Each network attempt is durably reserved before dispatch. Only complete sealed
request/response records with exact parameters, original acquisition times and
matching content digests can be considered for reuse. Every reused response is
validated again. A raw file without a completed journal record is not reusable.
The separate controller-owned seal is written atomically and fsynced. It assumes
a trusted local controller/filesystem and does not defend against coordinated
malicious rollback of both the journal and its trusted seal.

Only this run's fixed historical log ranges and event-timestamp headers are
eligible for reuse. Recovery rechecks the original SSZ identity and authenticates
the seed, target, direct child and finalized head with new source reads. Balances
and the final seed/target/child rereads are always fresh, never historical-cache
hits. Source freshness remains 90 minutes at acquisition, recovery, completion
and publication; economic compatibility remains five minutes.

At most four acquisition callbacks may remain active. An old owner cannot close
its journal or hand ownership to a replacement while callbacks are still live.
Unknown requests from a genuinely terminated process remain counted even though
they no longer occupy live worker slots. Cancellation, rejected review, corrupted
progress, changed inputs or exhausted bounds cannot create a successful result.

The original bounded same-source `eth_getLogs` -32005 splitting rule remains the
only result-limit retry path. An error response is never reused as successful
data. All queue, accounting, configuration, consensus and FIFO checks still run
to completion. Publication still requires the original two-file atomic gate and
public readback. Paused or local-only output cannot be published, and a completed
or failed run cannot be reopened or relabeled as a new run.

## Backlog and bounded work

The collector can replay across missed runs; seed age alone is not an error.
Log ranges use 1000-block chunks with bounded splitting. The ordinary
256-RPC-call budget includes timestamp-authenticating event headers and finality
polls, not just log requests. A busy day or a long outage can exhaust it, in which
case the prior checkpoint is retained and a bounded backfill/review is needed.
Do not silently drop events, advance a partial checkpoint or enlarge source
access to make a daily run appear successful.

Only the unique event headers whose timestamps affect the model are fetched
concurrently, with at most four requests in flight. IDs, admission, evidence and
the original lease's call budget are shared and synchronized. The header batch first reserves
six calls for the three same-block balances and final seed/target/child rereads;
those final rereads are always new requests. An error stops new admission and
discards the batch. A read returning after the deadline is rejected, and the
final result has an independent deadline check. Already admitted reads may finish
network teardown after cancellation; their late responses cannot produce a
successful result or trigger more reads.
The work directory retains a bounded `rpc-audit.json` even on acquisition
failure, including the actual request parameters and any still-in-flight IDs.
Cancellation by another request is distinguished from a source's own error.

Every real RPC admission, across all methods and workers, is spaced at least
2.0 seconds after the preceding admission. This fixed 0.5 requests/second pace
is a conservative engineering choice, not a provider quota or an availability
guarantee. Idle time does not accumulate burst credit. Waits release the shared
admission lock and are interrupted by cancellation or source failure; deadline
checks run again before charging and dispatching a request. HTTP 429 remains
terminal, with no retry or backoff loop. The original 25-minute compute budget
includes pacing time, while the 35-minute lease and 90-minute source-age limit
remain unchanged. Same-run resume restores the last admission from the sealed
journal's original monotonic clock, including failed or unknown attempts;
neither the clock, call budget nor pacing history resets on resume. There is no
CLI or environment override for the production pace.

Log acquisition, queue replay and the consensus/FIFO models retain their previous
semantics. Logs and canonical/finalized execution headers are provider-attested,
not proofs of every historical receipt or independently verified BLS finality.
Agreement between bounded samples, repeated queries or split ranges does not
prove all historical logs are complete. The collector still rejects missing responses, inconsistent hashes, missing
queue IDs, unsupported changes or invalid accounting, and retains the historical configuration's conditional provenance.

## Offline verification

```sh
PYTHONPATH=scripts/lido python -m unittest discover -s tests -p 'test_*.py' -v
node tests/lido-cutoff.test.js
```

Tests cover daily repeats and UTC midnight boundaries, manual-before-daily and
daily-before-manual, manual retry collisions, active leases, source denial,
source-age checks, unchanged economics restrictions, failure preservation and
atomic publication. Optional raw-state historical regression requires an
external audit fixture; it is not required for routine fresh collection.
Concurrency tests additionally cover out-of-order responses, request-ID races,
global budget admission, cancellation/denial, late responses, redirects and
equivalence with sequential event replay.
