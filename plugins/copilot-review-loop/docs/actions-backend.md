# Opt-in Actions backend

Select **Actions Copilot Review Loop** explicitly. The existing Copilot Review
Loop and PR Pipeline still use Agent Tasks. Neither path falls back to the other.
Actions inference runs Copilot CLI through gh-aw in private
`trask/copilot-workflows`, not a cloud Agent Task.

The adapter supports public, same-repository, trask-authored
`open-telemetry/opentelemetry-java-instrumentation` PRs for preview/shadow only,
and personal `trask/copilot-review-loop-test#1`. It does not support forks,
arbitrary repositories, or upstream publication. Central admission enforces
author and source identity. A preview still requires findings at the frozen
head; it freezes data without inference. Shadow spends central inference and
validation budgets but does not publish.

Newly frozen central requests default to a budget of five total model/worker
pipelines, including the first run, not five retries. Personal publication can
use that budget; shadow still performs one investigation per request.
Each request freezes its own `budgets.max_iterations` and matching
`publication.max_pipelines`, with a 7200-second elapsed cap. The current default
does not change an existing phase. Historical phases frozen with a two-run
budget remain at that limit. A phase exhausted at 2/2 stays exhausted, and this
adapter never reopens it or resets its count, bound, or deadline.

Find `scripts/actions_review_loop.py` inside the installed plugin. Use
`python3` if needed. All commands select `--backend actions` explicitly:

```text
python <helper> launch open-telemetry/opentelemetry-java-instrumentation#12345 --backend actions --mode shadow --receipt <fresh-absolute-path-outside-checkout>
python <helper> launch trask/copilot-review-loop-test#1 --backend actions --mode preview --receipt <fresh-absolute-path-outside-checkout>
python <helper> reconcile --backend actions --receipt <same-absolute-path>
python <helper> status trask/copilot-review-loop-test#1 --backend actions
python <helper> status <target> --backend actions --request-id <32-hex-ID> --generation <positive-integer> --revision <40-hex-trusted-workflow-SHA>
python <helper> cancel <target> --backend actions --request-id <32-hex-ID> --generation <positive-integer> --revision <40-hex-trusted-workflow-SHA> --receipt <fresh-absolute-path-outside-checkout>
```

Launch returns a durable local receipt before sending one POST to central main.
Acknowledgment means queued, not started or completed. A timeout or lost response
leaves the receipt uncertain. Reconcile reads at most 20 central coordinator
runs once and never repeats the POST. It reports possible run URLs, not a
target/request binding. Central currently generates request IDs inside the
coordinator and does not issue a dispatch-to-request receipt. Do not adopt the
latest checkpoint or a run title as proof that your launch owns that request.
Use the identity actually established by trusted central operation evidence.
An ordinary launch may return an existing active checkpoint, including a
different mode. That is not evidence of a new or shadow request.

Status reads only one checkpoint from a pinned `review-loop-state` commit.
It validates JSON schema, target IDs, request digest, workflow revision, worker
request title, and remote run attempt identities. It never downloads artifacts,
reads logs, runs target commands, or accepts Agent Tasks output. Report/state
data comes only from the private central GitHub API, never a local report file
or model stdout. Status without the three expected identity fields is discovery.
Supplying them rejects stale or replaced requests.

Status distinguishes the worker's original `frozen_sha` from central's current
`expected_sha`, and links the verified worker and verifier/validator runs.
Those are central observations, not a local check of the target branch.

Cancellation requires the published, source-pinned central exact-request
transactional guard. A mismatch fails before dispatch. Central checks the
request and generation inside its state transaction and increments generation
when active cancellation is admitted. It retains prior stage/reason and existing
intents/receipts. Other terminal evidence cannot be cancelled. An exact status
already marked cancelled at the current generation is a no-op; the adapter
reports it without POST or writing another receipt. Cancellation fences future
work, not already admitted remote effects. A queued cancel is not a confirmed
cancellation.

The guard pins CLI, coordinator, state transaction, and workflow bytes qualified
at `c82e5d9a86a183e8357539f219a28785ae0638ef`. Changing those trusted sources
requires a reviewed plugin pin update before cancellation is available again.
Operator-only repair, reconciliation, and `continue-personal-test` operations
are not exposed by this client.

## Personal test publication

Publication requires a separate explicit authorization for the test repo:

```text
python <helper> start-publication trask/copilot-review-loop-test#1 --backend actions --request-id <prior-terminal-request-ID> --generation <prior-generation> --revision <prior-trusted-workflow-SHA> --authorize-personal-publication --publication-auth fine_grained_pat --receipt <fresh-absolute-path-outside-checkout>
```

Only owner user ID `218610` can start this phase. The central publisher uses
the separately configured `REVIEW_LOOP_TEST_PUBLISH_TOKEN`, not
`COPILOT_GITHUB_TOKEN`. The adapter does not read, forward, create, or install
either credential. It uses existing ordinary `gh` authentication to access the
private central repository. Missing private access is an error, not proof of
capability. Publication remains subject to central credential, Copilot
capability, exact-head validation, CI, revision, and consumed-budget gates.
It never resets those gates or repairs/replaces a pipeline automatically.

Central workflow files, trusted scripts, checkpoints, credentials, candidate
packaging, validation, publication, bot-thread effects, fresh review, and CI
watching stay central. The helper performs no target mutations. Publication
does not authorize replies to real users or any OpenTelemetry mutations.

## Reading results

Every response is JSON with `backend: "actions"`. `central_stage` and `reason`
are central observations, not local acceptance. `pipeline_success` stays false:
this adapter does not grant pipeline clearance. `preview_complete` is freeze
completion, not review clearance. `shadow_complete` does not mean published.
`blocked` with `validation_unqualified` stays blocked even when
`objective_status` is `passed` and `general_qualified` is false.

Pending dispatch, inference, validation, publication, review, and CI remain
pending. Human gates, failed, cancelled, and exhausted outcomes stay explicit.
Successful Actions conclusions alone never establish clean. The approximate
five-minute central watcher handles due work; there is no local polling model,
sleeping runner, automatic recovery, or backend switch.

Receipts contain private central linkage, not tokens. Keep them outside public
checkouts. Each GitHub CLI subprocess has a 60-second timeout, bounded stdout
and stderr, no-window Windows launch, and no echoed authentication diagnostics.
