# Independent manual Lido runtime

This controller is a prepared, manual-only GitHub Actions route for the existing
Lido collector. It does not replace or change the external daily task or the
independent price workflow. Deploying the prepared files and requesting a real
run require the user's approval. No GitHub-runner source connectivity or Pages
build has been tested during local preparation.

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
pushes, schedules, pull requests, and other servers.

The optional manual **Run workflow** button reads the same committed request. It
does not generate a new ID, change a budget, or override deduplication. Re-running
an already consumed ID, including a failed ID, skips without source reads. Shared
durable leases block overlap with the existing external scheduled collector. A
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
The production run inherits its budget from the read-back durable manual lease.
The next ordinary lease defaults to 256 again.

The controller never calls `resume`. An eligible `paused` result is deliberately
aborted in the same job under the original journal/lease guards because this
workflow has no independent reviewed-same-route attestation. It waits for the
collector process group to stop before abort/publication. It does not move a
journal between jobs or runners. True source 401/403 and redirects remain terminal
under the collector's existing classification; there is no source fallback.

## Publication and token boundaries

The controller verifies its actual local collector/model/controller bytes and
workflow against current `main`, reads both input files at that exact commit,
and acquires one manual lease. Every repository mutation uses the GraphQL
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
It also records fresh-main/data/code identities, exact-head CAS and commit
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

Controller tests cover explicit-request gating, duplicate/failed-ID consumption,
shared leases, manual 320/default 256, strict head CAS, blob/code/request races,
uncertain-write reconciliation, expired lease refusal, failure retention,
same-job pause/abort, token isolation, process stopping and Pages mismatch. They
mock every network call and do not prove GitHub-runner source connectivity.

Official API and behavior references:

- [GitHub GraphQL commit inputs](https://docs.github.com/en/graphql/reference/commits)
- [Request a GitHub Pages build](https://docs.github.com/en/rest/pages/pages#request-a-github-pages-build)
- [Workflow triggering and GitHub-token events](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow)
