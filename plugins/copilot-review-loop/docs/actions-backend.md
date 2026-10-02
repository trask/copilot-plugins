# Opt-in Actions backend

Select **Actions Copilot Review Loop** explicitly. The existing Copilot Review
Loop and PR Pipeline still use Agent Tasks. Neither path falls back to the other.
Actions inference runs Copilot CLI through gh-aw in private
`trask/copilot-workflows`, not a cloud Agent Task.

Pass the PR you want reviewed as a GitHub URL or `owner/repo#number`.
The adapter does not choose a language, build tool, repository, or validation
command. Central workers read repository instructions, investigate the review
findings, make fixes, format them, and run appropriate checks.
Central admission binds the target and actual head repository. Private and fork
access depend on effective central credentials, not the URL alone.
Preview freezes findings without inference. Shadow spends central inference and
validation budgets but does not publish.

Newly frozen central requests default to a budget of five total model/worker
pipelines, including the first run, not five retries. Publication can
use that budget; shadow still performs one investigation per request.
Each request freezes its own `budgets.max_iterations` and matching
`publication.max_pipelines`, with a 7200-second elapsed cap. The current default
does not change an existing phase. Historical phases frozen with a two-run
budget remain at that limit. A phase exhausted at 2/2 stays exhausted, and this
adapter never reopens it or resets its count, bound, or deadline.

Find `scripts/actions_review_loop.py` inside the installed plugin. Use
`python3` if needed. All commands select `--backend actions` explicitly:

```text
python <helper> launch owner/repo#123 --backend actions --mode shadow --receipt <fresh-absolute-path-outside-checkout>
python <helper> launch https://github.com/owner/repo/pull/123 --backend actions --mode preview --receipt <fresh-absolute-path-outside-checkout>
python <helper> reconcile --backend actions --receipt <same-absolute-path>
python <helper> status owner/repo#123 --backend actions
python <helper> status <target> --backend actions --request-id <32-hex-ID> --generation <positive-integer> --revision <40-hex-trusted-workflow-SHA>
python <helper> cancel <target> --backend actions --request-id <32-hex-ID> --generation <positive-integer> --revision <40-hex-trusted-workflow-SHA> --receipt <fresh-absolute-path-outside-checkout>
```

Launch writes a durable local receipt before sending one POST to central main.
Acknowledgment means queued, not started or completed. A timeout or lost response
leaves the receipt uncertain. Reconcile reads at most 20 central coordinator
runs once and never repeats the POST. A schema-2 request records its actual
`launch_run`. Reconcile confirms ownership only when that owner/main/attempt-one
run, target, mode, and frozen revision match the dispatch receipt; possible run
titles alone do not bind a request. Otherwise it remains pending or unconfirmed.
Refreezes may retain the original launch run while changing the request revision.
The helper conservatively leaves that binding unconfirmed rather than guessing
the original revision from current main or a worker run.
Do not adopt the latest checkpoint or a run title as proof of ownership.
An ordinary launch may return an existing active checkpoint, including a
different mode. That is not evidence of a new or shadow request.

Status reads bounded JSON metadata from a pinned `review-loop-state` commit.
Schema-2 checkpoints use `pr-v2-<base-repository-ID>-<PR>.json`. A read-access gate
uses a separate case-folded repository-name hash until source identity can be
frozen. Numeric frozen checkpoints take precedence over these unfrozen gates.
Discovery rejects truncated trees, more than 1000 files, files over 1 MiB, more
than 16 MiB total, or more than 20 matching PR checkpoints. It reads only matching
checkpoint files, not every state or archive.

The adapter validates base and actual head repository IDs, visibility, branch,
request digest, workflow revision, worker request title, and remote attempts.
Native command receipt identities bind the candidate, source, complete plan,
setup, command exits, and log hashes. The helper does not download those logs
or artifacts or independently qualify commands. All verification and execution
remain central. It never accepts Agent Tasks output, local report files, or model
stdout. Status without the three expected identity fields is discovery.
Supplying them rejects stale or replaced requests. Schema-1 states and reports
are inactive, read-only historical observations, never a new phase or mutation
source.

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
at `4f0b10b7e804b34d0f16be152d3e5f65c4e53307`. Changing those trusted sources
requires a reviewed plugin pin update before cancellation is available again.
Operator-only repair, reconciliation, and `continue-personal-test` operations
are not exposed by this client.

## Publication authorization

Publication requires separate explicit mutation authorization for the target:

```text
python <helper> start-publication owner/repo#123 --backend actions --authorize-publication --publication-auth fine_grained_pat --receipt <fresh-absolute-path-outside-checkout>
python <helper> start-publication owner/repo#123 --backend actions --request-id <prior-terminal-request-ID> --generation <prior-generation> --revision <prior-trusted-workflow-SHA> --authorize-publication --publication-auth fine_grained_pat --receipt <fresh-absolute-path-outside-checkout>
```

Use the first form only when there is no existing frozen phase. Otherwise all
three prior identity fields are required and must name the exact terminal phase.
Legacy states cannot supply that authorization. No prior tuple means a new
phase request, not permission to replace or reopen a checkpoint.

Only central owner user ID `218610` can dispatch new work, and the PR must be
that actor's open PR. Central public reads use its Actions authentication.
Private reads require configured `REVIEW_LOOP_SOURCE_READ_TOKEN` access to the
actual base and head. Missing reads create
`human_gate_target_repository_read_access`; the adapter does not probe credentials.

The publisher uses the separately configured `REVIEW_LOOP_TEST_PUBLISH_TOKEN`,
not inference `COPILOT_GITHUB_TOKEN`. The historical secret name is unchanged,
but the generic publisher must authenticate as the launch owner and prove actual
base read/review and head push access. Base access on a fork does not grant head
access. Its currently configured credential remains personal-test-only; generic
routing does not authorize publication in other repositories.

Missing push/review access creates
`human_gate_target_repository_push_and_review_access`. Central also gates on
chosen-credential Copilot capability, credential-free candidate validation,
non-force exact-head push, fresh exact-head review, and selected non-Copilot CI.
No or missing CI cannot establish clean. The adapter never resets these gates,
probes target credentials, or repairs/replaces a pipeline automatically.
It uses ordinary configured `gh` authentication only for private central API
access and never reads, forwards, creates, or installs credentials.

Central workflow files, trusted scripts, checkpoints, credentials, candidate
packaging, validation, publication, bot-thread effects, fresh review, and CI
watching stay central. The helper performs no target mutations. Publication
does not authorize replies to real users, metadata changes, merges, or force pushes.

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
