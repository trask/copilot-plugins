# copilot-plugins

GitHub Copilot CLI plugins that help you review pull requests.

## Install

Add the marketplace once:

```bash
copilot plugin marketplace add trask/copilot-plugins
```

Then install any of the plugins:

```bash
copilot plugin install agent-tasks-runtime@trask-plugins
copilot plugin install pr-reviewer@trask-plugins
copilot plugin install copilot-review-loop@trask-plugins
copilot plugin install self-review-loop@trask-plugins
copilot plugin install pr-description@trask-plugins
copilot plugin install pr-pipeline@trask-plugins
copilot plugin install pr-conflict-resolver@trask-plugins
copilot plugin install ci-fix-loop@trask-plugins
copilot plugin install historical-pr-audit@trask-plugins
```

Restart Copilot after you install or update a plugin.

`pr-reviewer`, `copilot-review-loop`, `self-review-loop`, `pr-description`,
`ci-fix-loop`, `historical-pr-audit`, `pr-pipeline`, and `pr-conflict-resolver` require
`agent-tasks-runtime@trask-plugins`. The runtime contains no custom agents and
does not add anything to the agent list. PR Conflict Resolver keeps its
specialized hosted conflict backend inside its own plugin. It uses the shared
runtime only for local execution ownership.

## Controller execution

All nine agent entrypoints launch their existing foreground Python controller
once. The shared Runtime execution library owns process generations, child
launches, durable output and explicit local cancellation. Controllers keep their
workflow-specific stages, permissions, candidate checks and budgets. Hosted
agents still diagnose, edit and validate.

Except for sealed CI, pass a fresh absolute path outside the checkout:

```text
python <installed-helper> <workflow-command> <target> --execution-handle <absolute-path>
python <installed-helper> execution-status --handle <same-absolute-path>
python <installed-helper> execution-cancel --handle <same-absolute-path>
```

Pipeline and Stack use `run`, Reviewer uses `run` with explicit
`--post-pending-review` authority, and other standalone workflows use
`agent-task`. Sealed CI derives its handle from its v3 artifact. Its controls
accept that exact artifact instead of `--handle` and retain original-session
admission. Runtime remains an internal dependency, not a standalone workflow.

Use the official execution tool's documented asynchronous mode. Tool-level
detachment requires the user's explicit request to continue after client exit.
Do not add another detach layer. The tool acknowledgement does not prove
readiness, and tool-shell exit does not prove workflow completion. Optional
status reads verify process generation and the canonical terminal-result hash;
they do not drive the workflow. The handle's `.d` directory holds stdout,
stderr, progress, child evidence and results. An unsealed root observed only
after its executable image becomes unavailable remains abandoned and remotely
unconfirmed.

Client or observer disconnection is not cancellation. Explicit plugin cancellation
fences new launches and publication, but cannot undo an admitted remote request
or promise hosted-task cancellation. Failed and cancelled runs retain unknown
or active task identities, spent budgets and artifacts. Branch admission is based
on current execution evidence rather than persistent writer reservations. There
is no automatic takeover or recovery, and a fresh invocation never adopts an
unresolved historical task.

The execution record distinguishes finished, failed, locally cancelled and
abandoned runs. A finished controller may still report pending review, exhaustion,
blocked stages or CI warnings. No candidate or process exit means CI green.
Windows children use no-window launch, native generation checks and job
membership. The suspended direct child is bound to its exact handle, job,
generation and image before resume. After exit, that retained binding and the
handle's signaled state prove identity without requiring image data to remain
available. The verified owned job is the completion unit, so a job member may
briefly outlive the direct process while normal completion remains pending.
Success still requires complete verified membership and a zero active-job count
within the original deadline. Unverified or forced drainage is an operation
failure even when cleanup later proves that the job reached zero. An open
failure for an enumerated member is retried only when a second Job Object
snapshot proves that membership changed. A live image query may be retried on
the same generation-bound handle only while repeated Job Object snapshots
prove exact stable membership; persistent denial remains a failure. A
foreground timeout covers communication and owned-job drainage, and
cancellation remains observable while drainage is pending.
Linux uses procfs generations and owned process groups; unresolved
descendant drainage is reported rather than assumed.
Other process-generation providers are unsupported.

This is not an app task manager. It promises no app-native Stop integration,
graceful app-shutdown survival, automatic recovery or notifications after exit.
Only one inert direct foreground process has been qualified across controlled
client termination on the tested Windows host. Production trees, graceful
cleanup, app exit, post-exit streams and remote cancellation remain unqualified.
An earlier native matrix failed in job accounting for a cause that remains
unexplained. A separate later cf40629 nested-process matrix stopped on its first
case when a post-exit image query returned WinError 31. Independent handles
prove that the direct and nested processes exited, but the library did not
confirm job drainage. A 9a79 matrix then found verified live `conhost.exe`
members after both Python generations exited and failed because the library
immediately forced job drainage. That evidence does not explain why those
members existed or how long they would have remained naturally. None of these
failures qualifies cases that did not run, and mocked coverage is not native
qualification.

## Plugins

### Agent Tasks Runtime

Provides the shared, hash-verified `cloud_task.py` used by six Agent Tasks
agents. The plugin exposes no custom agent; consumer coordinators locate its
internal skill through Copilot's skill inventory and fail closed when the
runtime is missing, disabled, or incompatible.

The executable cloud-task surface supports only the current version-5
code-candidate and report-recommendation contracts. Retired apply/report,
resume, monitor-only, prepared-result and task-reuse options fail before
dispatch or mutation; historical artifacts remain evidence and cannot be used
to resume a fresh invocation.

### PR Reviewer

Uses one hosted Sol task to discover findings in the pinned pull request.
Nonempty discovery starts one separate hosted Astra task to critique the whole
batch and draft the retained comments. Empty discovery needs no second task.
The local coordinator checks provenance, anchors, permissions and freshness,
then creates one pending review containing the exact hosted comments.

Run this agent with GPT-6 Sol at high reasoning effort. Both hosted phases use
`marketplace-agent-report-recommendation-worker@1`. There is no hosted
max-effort guarantee, local evaluator, per-finding task or model fallback.

### Copilot Review Loop

Works through the Copilot pull request review comments that nobody has resolved
yet. It groups comments that share one cause into one commit, pushes the fixes,
and asks Copilot to review again when the current head has no clean review. It
repeats until the review is clean or it reaches a stop condition.

Five spent fixes with remaining feedback is terminal but unresolved. Pipeline
continues Self Review, CI and Description without clearing Review or granting
another allowance. Later sweeps retain the pending feedback and spent budget.

The plugin verifies the shared Agent Tasks runtime. Authentication stays in
local `gh api`; repository analysis and execution stay in GitHub Agent Tasks.
Local candidate validation compiles the exact runtime source bytes whose digest
it verified. It neither reads nor writes installed Python bytecode caches.

### Self Review Loop

Uses managed GitHub Agent Tasks to review the pinned pull request and fix
findings in scope. Formatting, tests, and builds stay hosted. The local
coordinator validates committed code and the live branch identity before it
imports and pushes fixes. One hosted loop receives the remaining review-pass
allowance and returns `clean`, `exhausted` or `incomplete` with the passes used.
A clean result may include fixes. Zero commits alone does not establish clean.
Hosted passes, Runtime tasks and source publications are separate counts.

The plugin locates and verifies the shared `cloud_task.py`, then runs it with
policy `marketplace-agent-code-candidate-worker@1`. The backend is GitHub Agent
Tasks through local `gh api`; there is no Cloud Sandbox, custom agent, local
repository analysis, or local execution fallback.

### PR Description

Uses a managed GitHub Agent Task to review the current pull request title and
description against the complete diff. The local coordinator pins the pull
request and permission context, then reads the proposed title and body from
committed files on the task's generated branch. It keeps matching text or
applies the proposed replacement through GitHub's authenticated API when
permission allows it.

The plugin locates and verifies the shared `cloud_task.py`, then runs it with
policy `marketplace-agent-report-recommendation-worker@1`. The backend is GitHub
Agent Tasks through local `gh api`; there is no Cloud Sandbox, custom agent, or
local-analysis fallback.

Description deliberately invalidates same-head clearance when metadata or the
actual base tip changes. A title or body change at the same head can be
reevaluated only in a strictly later authorized Pipeline iteration after the
earlier task completed. A source-only replacement proposal remains excluded,
not cleared.

### PR Pipeline

Runs conflict handling, Copilot review, self review, CI repair, and description
validation for one explicitly selected open pull request, draft or ready for
review. The same plugin includes PR Stack Pipeline,
which applies those existing agents to a selected suffix of a native GitHub
stack with at most two passes.

Schedulers invoke stage coordinators directly, preserving their exit codes.
Each stage waits for its children and owns its configured iteration budget.
Another Pipeline pass does not replenish that budget. An interrupted run
fails; a later invocation starts from the beginning.

Both parent agents launch their foreground scheduler once. Durable progress and
terminal files do not depend on a local agent keeping a watch loop alive.
Observers are optional and read-only.

PR Flight starts the stack agent with one JSON object:

```json
{"version":1,"repository":"owner/repo","stackNumber":77,"startPullRequest":11,"pullRequests":[11,12]}
```

`pullRequests` is the ordered, base-to-tip selected suffix beginning at
`startPullRequest`. The helper checks that identity and order against the live
native stack before it starts. After the run, the session is named
`PR Stack Pipeline: #<startPullRequest> - <PR title>` from the starting pull
request's live metadata.
Stack state lives under `~/.copilot/run/pr-stack-pipeline/` and exposes the run
ID, topology fingerprint, selected suffix, expected heads and bases, current
pass and phase, per-PR stage state, dispatch nonces, result, and timestamps.

Normal execution defaults to `--github-mutation-policy allow`. Requesting a
Pipeline run authorizes its standard source fixes, Copilot review requests,
bot-thread replies and resolution, and title/body updates on the selected PRs.
It does not authorize merging, approval, changing draft state, unsolicited
comments, or replies to human-authored threads.

Use `source-only` only when explicitly requested or when the caller forbids
the normal review or metadata updates, not merely because a PR is a draft.
Under `source-only`, guarded source publication is allowed, but comments,
reviews, review requests, thread changes, workflow reruns, and PR or stack
metadata changes are not. A proposed description replacement remains blocked
under this policy; it does not count as a cleared description stage.

### PR Conflict Resolver

Resolves the merge conflicts on a pull request in one pass. It reads the history
behind each conflicted file first, then keeps what both sides meant to do rather
than picking a side. It stops and reports
when the two sides genuinely contradict each other. It refuses to rewrite an
ordinary branch with dependents. For a native GitHub stack, the coordinator
starts each member's hosted task at its verified new base. The worker replays
only that member's commits onto its assigned branch. The coordinator collects
committed code from each task's authoritative branch and verifies the complete
result before publishing with one atomic, exact-lease push. A run that publishes and
then still reads as conflicting is finished rather than failed, and a caller
that wants another integration starts another run. It never posts anything to
GitHub. Its
machine-facing descendant propagation operation uses the same topology checks and
atomic publisher after a lower stack member receives a CI fix.

The source branch is authoritative before and after hosted work. If GitHub's
PR record lags a native restack, the resolver derives commits and conflicts
from the actual branch instead of blocking on that stale record. A real branch
change during the run still stops publication.

The plugin bundles and verifies its dedicated conflict Agent Tasks runtime.
Authentication stays in local `gh api`; conflict analysis and validation stay
in GitHub Agent Tasks.

### CI Fix Loop

Fixes only failures attributable to the pull request. A standalone run on a
native GitHub stack checks CI from the bottom member through the top, starts real
repair work at the lowest failure, and uses PR Conflict Resolver to propagate
each fixed head through its descendants before their checks run. It does not run
the review, description, or other PR Pipeline stages. A pull request outside a
native stack keeps the single-PR behavior.

The plugin verifies the shared Agent Tasks runtime. Authentication stays in
local `gh api`; the local controller investigates retained logs, while hosted
Agent Tasks diagnoses the failures against the checkout, edits, and validates.

The controller keeps attempt-bound logs on disk and uses its configured model
(Sol by default) to prepare a compact, free-form CI briefing. The hosted worker
receives that unverified briefing and a failed-check inventory, not the logs.
It checks suggested commands against its Linux checkout before running them.
Test moves, skip-like strings and wrapper edits are not local semantic vetoes;
the hosted worker must preserve test execution and coverage.

CI Fix Loop considers all checks, not just required checks. The hosted worker
diagnoses failures; the local controller rechecks live identity and permissions
before publishing fixes or requesting a failed-jobs rerun. Its one-retry
allowance per workflow run includes retries started by repository automation.
An explicit `source-only` policy blocks reruns without an empty-commit workaround.

Unrelated or pre-existing failures remain visible with the hosted worker's
reason and evidence. They can finish the stage with a CI warning, allowing
Pipeline to continue without claiming that CI passed. Unknown causes remain
unresolved. A retry or other CI change during hosted work invalidates the
candidate before publication.

Green and warning clearance both require fresh head, actual base, check and
workflow-attempt observations, including new same-head runs not yet in the
check rollup. Revalidation starts no hosted task and spends no repair allowance.

Each member gets five charged iterations. PR Pipeline does not reset this
budget between passes. Every accepted push records a machine-readable
checkpoint. Install `pr-conflict-resolver@trask-plugins` to use native-stack
mode; the loop checks for it before it edits or pushes any stack member.

### Historical PR Audit

Uses one managed GitHub Agent Task to audit a merged pull request against its
pinned historical base, head, diff, and discussion. The worker compares changed
areas with sibling implementations, fixes every validated finding, and runs the
historical tree's formatting, tests, and builds. The local coordinator validates
the task's commit history, outcome, pass count and repository identity before
it imports and pushes the fixes to `trask-pr-audit-<number>`.

It also compares each changed area with the closest sibling implementations in
that historical tree, and treats an unexplained departure from a strong,
directly applicable precedent as a finding worth raising.

The merged pull request never changes. The audit branch is the only thing this
agent pushes, and a first pass that finds nothing pushes no branch at all.

The plugin locates and verifies the shared `cloud_task.py`, then runs it with
policy `marketplace-agent-code-candidate-worker@1` with an explicitly bound
merged historical source. Its required `audit-result.json` contains only
`outcome` and `iterations_used`; optional prose is not acceptance evidence.
Every top-level invocation is fresh. Retained validated or publication-failed
state cannot be re-entered. Exact-ref confirmation of a lost push response is
allowed only inside the still-active invocation. The backend is GitHub Agent Tasks through
local `gh api`; there is no Cloud Sandbox, custom agent, local repository
analysis, or local execution fallback.

### Optional PR Flight State Sharing

Self Review Loop and PR Description can copy the few completion facts that
the PR Flight canvas uses to a private GitHub repository. That keeps those
stages the same on every machine you use. Set
`COPILOT_PR_FLIGHT_STATE_REPO=owner/repo`, or install a PR Flight extension that
writes `~/.copilot/extensions/pr-flight/state-repo.json` with a `repository`
value. An environment variable that is set but empty turns sharing off. When
sharing fails you get a warning, and neither workflow fails.

## Retrospectives

Every agent ends a run by looking back at how the run itself went, and reports
concrete friction you could remove. It tags each report as a change to the agent
definition, the bundled helper script, your general Copilot instructions, or the
reviewed repository's own instructions. The reports are advice in chat only.
Each one comes from friction the agent actually hit in that run, so a run that
went smoothly reports nothing. A run that stopped early still reports, because
that is where friction shows most clearly.

## Update

```bash
copilot plugin marketplace update trask-plugins
copilot plugin update agent-tasks-runtime
copilot plugin update pr-reviewer
copilot plugin update copilot-review-loop
copilot plugin update self-review-loop
copilot plugin update pr-description
copilot plugin update pr-pipeline
copilot plugin update pr-conflict-resolver
copilot plugin update ci-fix-loop
copilot plugin update historical-pr-audit
```

## Requirements

- GitHub Copilot CLI
- GitHub CLI (`gh`), signed in for the repositories you review
- Python 3.10 or newer

## License

[MIT](LICENSE)
