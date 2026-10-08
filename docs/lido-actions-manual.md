# Daily and explicit manual Lido runtime

The existing GitHub Actions controller runs the Lido collector once each day at
**00:00 UTC**, or for one explicitly requested manual attempt. Both routes use
the same workflow, concurrency group, durable lease, model and exact-head
publication gate. The existing two-minute price relay also checks whether the
current UTC day needs a daily fallback dispatch; it does not collect Lido data.

Before activating the repository schedule, the old external daily task must be
paused through its supported owner controls. A denied task-control request must
not be worked around through another route or a repository gate. Local code and
offline tests do not themselves activate a schedule or establish automatic-run
acceptance. The user confirmed the old task was paused on 2026-10-07; that is an
owner confirmation, not an API-verified task state.

## Daily event, UTC-day identity and acceptance

`.github/workflows/lido-manual.yml` has exactly one scheduled expression:
`0 0 * * *`, using GitHub's default UTC timezone. It does not add a noon/evening
cadence, dispatch itself, or create an automatic manual request. Its existing
`lido-explicit-manual` concurrency group now serializes both trigger types with
`cancel-in-progress: false`; the shared durable lease remains authoritative.

Only a genuine `GITHUB_EVENT_NAME=schedule` with the exact cron and original
`GITHUB_RUN_ATTEMPT=1` can acquire a scheduled lease. The controller reads this
public repository's original workflow-run metadata without authentication and
checks the run ID, event, attempt, workflow path, main branch, repository and
event head SHA. It adds no token permission. A missing, denied, mismatched or
unavailable provenance read stops before any lease write or source collection.

The attempt key is the UTC date of that original run's `created_at`, explicitly
recorded as `utcDayBasis=github_run_created_at`. It must still be the current UTC
day at lease acquisition, every acquisition CAS attempt, and immediately before
launching the collector process. A run queued across midnight cannot consume the new day's
key; a rerun cannot become a new daily attempt. A lease acquired just before
midnight whose collector has not yet launched is released as a failed old-day
attempt without source collection. Once the collector has launched, its original unexpired lease
and source-freshness requirements still govern completion/publication.
This boundary does not claim the child process's first download byte arrives
before midnight; the collector and its acquisition rules are unchanged.

GitHub documents that scheduled events can be delayed or dropped, especially at
the start of an hour. It exposes the cron expression but no documented nominal
occurrence timestamp. Therefore creation-day evidence does not establish the
intended nominal cron date after an extreme delay before run creation. The
controller never guesses a missed day or backfills it. Same-day delays can run
once; missing days stay missing. Midnight remains the configured cadence.

The collector's existing `attemptDay` prevents a second scheduled collection on
the same UTC day, including after failure. Scheduled leases always select the
ordinary 256 RPC attempts, even if the manual request file is inert, malformed,
or still records an earlier approved 320 catch-up. The manual file remains in
the unchanged-file fence but is not scheduled intent or scheduled configuration.
An active manual or scheduled lease blocks the other trigger without source
reads or replacement dispatch by the receiver.

### Existing relay fallback

The relay's `scripts/lido-daily-dispatch.js` reads current `main` snapshot state,
checks all noncompleted Lido workflow statuses, and re-reads the snapshot before
sending. A consumed UTC `attemptDay`, source-access block, or active lease prevents
dispatch. It submits `mode=daily`, the UTC `attempt_day`, and its `relay_run_id`
using its existing ephemeral `GITHUB_TOKEN` with `actions:write`. No new token,
permission or manual request is created. Each API request has a ten-second limit.
An API/input failure or uncertain dispatch stops further checks for that UTC day
in the current relay process; a later process checks durable state again.

The receiver validates the original dispatch run and referenced snapshot relay
metadata and GitHub Actions bot actor, rejects historical completed relay IDs,
reruns and cross-day delivery, and records `relay-dispatch`
as the trigger in the durable lease, refresh metadata and diagnostic audit. The
audit's `scheduledRequest` field carries this daily identity, including the relay
run ID and dispatched day; it does not claim a native cron event occurred.
Cron and relay consume the same `attemptDay` by the same exact-head atomic lease
acquisition before any source work. A race between send/preflight and cron can
enqueue a redundant workflow but cannot start duplicate collection. Success or
failure consumes the day; the relay cannot turn it into a manual retry or 320
budget. Both daily routes retain 256 calls, 1,500 compute seconds and a 2,100-second
lease, with the existing model, pacing and finite source timeout retries.

Updating `loop.sh` does not update a running shell. The next relay process must
start from the new main revision before acceptance; a normal relay handoff does
this. Verify its daily dispatch, receiver lease, complete dual-JSON publication
and public byte equality separately from the cron acceptance described below.

First automatic acceptance requires an actual GitHub run with `event=schedule`
after activation, not a manual dispatch or synthetic fixture. Decode its complete
standard audit and verify `githubEventName`, `githubRunId`, `githubRunAttempt`,
`trigger=scheduled`, `scheduledRequest` (cron, creation time/day, head, 256 budget),
and the read-back lease. Then verify the model/RPC evidence, exact two-file
publication and separate public Pages byte equality as usual. A skipped/failed
event does not prove a successful automatic refresh.

## Explicit request and deduplication

The committed `.github/lido-manual-request.json` is initially inert:

```json
{"schema":1,"requestId":null,"rpcCallBudget":256}
```

After one specific run is authorized, change `requestId` to that request's stable,
non-sensitive ID. Keep `rpcCallBudget` at 256, or use 320 only for the specifically
approved one-off catch-up. Commit that file to `main`. The workflow's only push
path is this request file. A normal price/data/code push cannot start collection.
The controller independently compares the before/after event trees and rejects
a push that did not change the request file. It rejects forks, other refs, force
pushes, pull requests, and other servers. The separate daily route is described
above; dispatch with omitted/default `mode=manual` remains manual. Only the
explicit `mode=daily` input selects the bounded relay fallback.

The optional manual **Run workflow** button reads the same committed request. It
does not generate a new ID, change a budget, or override deduplication. Re-running
an already consumed ID, including a failed ID, skips without source reads. Shared
durable leases block overlap between scheduled and manual collection. A
blocked or failed run never requests its own replacement. The existing 128-ID
history is bounded; the operator must never replay older consumed IDs.

Installing this inert file may create one no-op workflow run. Creating a new ID
is explicit manual intent and must not be automated by another recurring job.
GitHub-token pushes normally do not start another workflow: activation should be
the authorized user/repository change or an explicit manual dispatch, not a
self-dispatching bot chain.

## Original model and clocks

The one `ubuntu-24.04` job provisions Python 3.12 and checks for `g++` before
acquiring a lease. Official setup/checkout actions are pinned to their verified
release commit SHAs. The existing `refresh.py run` does all source acquisition
and calculation unchanged, including a freshly downloaded and fully authenticated
finalized SSZ, complete event queue, full model, and six independent tiers.

The original collector retains its 25-minute computation limit, 35-minute lease,
90-minute source freshness, five-minute economic compatibility limit, four-header
worker bound and fixed public sources. A 40-minute job timeout permits cleanup
and bounded Pages verification; it does not extend the collector or lease.
The production run inherits its budget from the read-back durable lease.
Cron and relay daily leases always use 256; a manual 320 request remains specific to that
explicit request. The next ordinary lease defaults to 256 again. Both routes
retain shared two-second RPC admission and the existing event-header-only
bounded read-timeout retries, with failed retries charged to the same budget.

The controller never calls `resume`. An eligible `paused` result is deliberately
aborted in the same job under the original journal/lease guards because this
workflow has no independent reviewed-same-route attestation. It waits for the
collector process group to stop before abort/publication. It does not move a
journal between jobs or runners. True source 401/403 and redirects remain terminal
under the collector's existing classification; there is no source fallback.

## Publication and token boundaries

The controller verifies its actual local collector/model/controller bytes and
workflow against current `main`, reads both input files at that exact commit,
and acquires one scheduled or manual lease. Every repository mutation uses the GraphQL
`createCommitOnBranch` mutation with `expectedHeadOid`. Each accepted commit's
single parent, entire tree and exact file bytes are read back and verified.

Only independent price files may advance during a calculation or a CAS retry:
`data/history.json`, `data/legacy/history-v1.json`, and dated archive files. Any
snapshot, checkpoint, lease, request, workflow, collector/model, UI or other change
stops this attempt. A head race gets at most three CAS attempts without repeating
collection; every attempt rechecks the lease and unchanged data/code. An uncertain
write gets one readback, never a blind mutation retry.

`publication.py` is imported unchanged as the final gate. Success writes exactly
the snapshot and checkpoint atomically. Failure writes only snapshot status and
releases the owned, unexpired lease while retaining all previous successful
scenario/evidence data and checkpoint. Expired or foreign leases cannot be
released by this job. Abrupt runner loss may prevent cleanup; its lease expires
naturally and the run must not be reported as successful.

Only the controller receives the automatically supplied, short-lived repository
token. Its permissions are `contents:write` and `pages:write`. There is no PAT,
new account, key, payment, `actions:write`, or persisted Git credential helper.
Collector subprocesses use an explicit environment allowlist excluding GitHub,
runtime/OIDC and other tokens, Python hooks, Git configuration, and proxies.
Credentials never enter command arguments, files, or logs. API redirects are
refused and response/error bodies are not logged.

## Pages verification

Before consuming the request, the controller checks the existing Pages site is
`build_type=legacy`, serves `/` from `main`, and uses the repository's expected
GitHub Pages URL without a custom domain. Unsupported configuration fails clearly;
the controller never changes Pages settings.

After a verified data commit it requests one new build with
`POST /repos/attm-agenticcoding/lst-peg-monitor/pages/builds`, then reads the actual
public snapshot and collector JSON until both match the committed bytes (at most
six minutes). It does not retry the old denied Pages workflow run. A 401/403 or
redirect stops; a mismatch/timeout reports deployment failure separately from an
already successful repository commit. It never alters a committed successful
model merely because Pages is unavailable.

The raw SSZ and run scratch exist only inside this job. No artifact upload, cache,
paid storage, or cross-job recovery is configured. After the collector process
group is confirmed stopped, `actions_audit.py` emits a gzip/base64 diagnostic
bundle into ordinary Actions logs. The exact whitelist contains the original
result manifests, complete model audit, RPC audit, sealed journal and independent
seal, plus only the raw RPC response files referenced by that verified journal.
It also records GitHub event/run provenance, scheduled creation-day identity or
manual request, fresh-main/data/code identities, exact-head CAS and commit
readbacks, six-tier/cutoff results, and separate Pages verification status.

Raw SSZ, environment variables, HTTP authentication headers, credential fields,
and token bytes are excluded. Symlinks, traversal, orphan responses, damaged
seals or response bytes are rejected. Limits are 64 MiB for the uncompressed
diagnostic archive and 8 MiB for gzip. The log contains numbered chunks and
matching begin/end byte counts and SHA-256 digests. Overflow or corruption fails
the diagnostic step explicitly; it never truncates a bundle while claiming
completeness or overwrites a previously established collector/Pages result. A
successful collector whose audit cannot be exported still fails the workflow's
overall acceptance. Unknown live child state skips export and retains its scratch
and lease; runner loss may leave no complete audit at all.

Download the workflow job's text log, then extract and verify all archive/file
digests with:

```sh
python scripts/lido/actions_audit.py --log JOB_LOG.txt --output NEW_AUDIT_DIRECTORY
```

The extractor accepts timestamp-prefixed Actions log lines, requires every chunk
and the final end record, limits decompression, and writes only validated paths
into a new directory. The bundle is diagnostic evidence; it is never accepted as
cross-runner recovery input.

## Local offline checks

```sh
PYTHONPATH=scripts/lido python -m unittest discover -s tests -p 'test_*.py' -v
node tests/core.test.js
node tests/redemption.test.js
node tests/collect-redemption.test.js
node tests/app-redemption.test.js
node tests/lido-cutoff.test.js
```

Controller tests cover genuine schedule provenance, rerun/wrong-cron rejection,
midnight boundaries, daily failure consumption and next-day eligibility,
scheduled 256 despite manual 320, explicit-request gating, duplicate/failed-ID
consumption, shared leases, manual 320/default 256, strict head CAS, blob/code/request races,
uncertain-write reconciliation, expired lease refusal, failure retention,
same-job pause/abort, token isolation, process stopping and Pages mismatch. They
mock every network call and do not prove GitHub-runner source connectivity.

Official API and behavior references:

- [GitHub GraphQL commit inputs](https://docs.github.com/en/graphql/reference/commits)
- [Request a GitHub Pages build](https://docs.github.com/en/rest/pages/pages#request-a-github-pages-build)
- [Workflow triggering and GitHub-token events](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow)
- [GitHub schedule semantics](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)
- [Read public workflow-run provenance](https://docs.github.com/en/rest/actions/workflow-runs#get-a-workflow-run)
