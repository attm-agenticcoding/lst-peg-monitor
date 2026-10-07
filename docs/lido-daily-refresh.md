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
automatic provider fallback, redirect following, API key or historical-response
cache. Switching this source requires explicit approval and source validation.

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
python scripts/lido/refresh.py run --run-id RUN_ID \
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

The collector deletes its large SSZ after saving the result manifest.
`--keep-source` is only for explicitly requested local audits. After verified
publication or deliberate discard, remove only that run's temporary files.

## Backlog and bounded work

The collector can replay across missed runs; seed age alone is not an error.
Log ranges use 1000-block chunks with bounded splitting. The existing global
256-RPC-call budget includes timestamp-authenticating event headers and finality
polls, not just log requests. A busy day or a long outage can exhaust it, in which
case the prior checkpoint is retained and a bounded backfill/review is needed.
Do not silently drop events, advance a partial checkpoint or enlarge source
access to make a daily run appear successful.

Only the unique event headers whose timestamps affect the model are fetched
concurrently, with at most four requests in flight. IDs, admission, evidence and
the 256-call budget are shared and synchronized. The header batch first reserves
six calls for the three same-block balances and final seed/target/child rereads;
those final rereads are always new requests. An error stops new admission and
discards the batch. A read returning after the deadline is rejected, and the
final result has an independent deadline check. Already admitted reads may finish
network teardown after cancellation; their late responses cannot produce a
successful result or trigger more reads.
The work directory retains a bounded `rpc-audit.json` even on acquisition
failure, including the actual request parameters and any still-in-flight IDs.
Cancellation by another request is distinguished from a source's own error.

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
