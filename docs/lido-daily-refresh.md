# Twice-daily Lido mechanism refresh

The authorized schedule is one attempt at **12:00 UTC** and one at **18:00 UTC**
each day, fixed UTC year-round (no daylight-saving adjustment). An explicitly
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
- Twice-daily schedule: `mode=twice-daily`, `scheduleUtc=["12:00", "18:00"]`
- `expectedIntervalSeconds=64800` is the longest scheduled gap (18 hours); the
  daytime gap is 6 hours, not a uniform 12-hour interval

Every newly acquired result must be at most **5400 seconds (90 minutes)** old
at acquisition, completion and publication. The display's operational
age allowance is separately `maxOperationalAgeSeconds=70200`, the longest
18-hour scheduled gap plus that source allowance. It does not permit publishing a day-old
source. Economic quote compatibility is still only 300 seconds, with the exact
queue block/time and actual purchased stETH amount required. A twice-daily model is
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

Scheduled invocation (replace the example with the original UTC occurrence
that caused this wake, and keep that same key on delivery/retry):

```sh
python scripts/lido/refresh.py prepare-lease \
  --scheduled-slot 2026-10-07T12:00:00Z --output /tmp/lido-lease.json
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
retry. Manual runs never clear or change the scheduled `attemptSlot` key.

Scheduled attempts deduplicate on `attemptSlot`, the exact occurrence timestamp
such as `2026-10-07T12:00:00Z` or `2026-10-07T18:00:00Z`. A failed scheduled
attempt consumes only that slot; the other slot and next day's slots remain
eligible. The latest slot alone is sufficient bounded history because older
slots can never be reacquired: acquisition must fall in the original slot's
one-hour start window, `[slot, slot + 3600 seconds)`. An early, missed or late
wake skips without backfill. Never relabel a delayed 12:00 wake as 18:00, infer
an old missed slot from the current day, or switch to manual to bypass this.
The command requires `--scheduled-slot`; the scheduled task must pin the
original occurrence provided by its wake context. If that identity is absent,
skip rather than inferring a newer occurrence from the current clock.
Legacy `attemptDay` and `attemptHour` remain historical metadata only and cannot
suppress either new slot. No historical attempt is converted into a new slot.

Manual attempts are independent of that key, but every trigger shares the
same active lease, so overlapping scheduled/manual runs cannot both acquire
ownership. If a scheduled wake encounters an active lease, it may retry lease
acquisition once that lease is released, within the same slot's start window.
The window does not extend the 35-minute acquired lease or 25-minute compute
limit, and does not permit stale source publication.

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
access to make a scheduled run appear successful.

## Offline verification

```sh
PYTHONPATH=scripts/lido python -m unittest discover -s tests -p 'test_*.py' -v
node tests/lido-cutoff.test.js
```

Tests cover both same-day slots, overnight and DST boundaries with fixed UTC,
delayed/early/missed wakes, daily/hourly key migration, manual/scheduled key
independence, manual retry collisions, active leases, source denial,
source-age checks, unchanged economics restrictions, failure preservation and
atomic publication. Optional raw-state historical regression requires an
external audit fixture; it is not required for routine fresh collection.

## Cadence rollout

Deploy the compatible occurrence-key collector/publication gate before changing
the existing external automation; do not create another task. The first new
occurrences after the 2026-10-06 cadence change are **2026-10-07 12:00 UTC** and
**2026-10-07 18:00 UTC**. Do not backfill October 6 or run an ad hoc collection.
A metadata-only cadence migration preserves all old scenario content, source
time, checkpoint, error and attempt history; it is not a successful refresh.
