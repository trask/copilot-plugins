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
`ci-fix-loop`, and `historical-pr-audit` require
`agent-tasks-runtime@trask-plugins`. The runtime contains no custom agents and
does not add anything to the agent list. PR Conflict Resolver keeps its
specialized conflict runtime inside its own plugin and does not require the
shared runtime.

## Plugins

### Agent Tasks Runtime

Provides the shared, hash-verified `cloud_task.py` used by six Agent Tasks
agents. The plugin exposes no custom agent; consumer coordinators locate its
internal skill through Copilot's skill inventory and fail closed when the
runtime is missing, disabled, or incompatible.

### PR Reviewer

Uses a managed GitHub Agent Task to inspect the complete pinned pull request
diff and run focused probes in isolation. The local coordinator validates the
task's committed report and receipt, then checks each candidate with a separate
fixed evaluator before it creates and verifies one pending review.

Run this agent with GPT-5.6 Sol at high reasoning effort. It checks each finding
with a separate GPT-5.6 Sol evaluator at max reasoning effort. The evaluator is
independent of the selected managed worker model. The plugin verifies the shared
Agent Tasks runtime and uses policy `marketplace-agent-worker@1`. It never uses
Cloud Sandboxes or a local-analysis fallback.

### Copilot Review Loop

Works through the Copilot pull request review comments that nobody has resolved
yet. It groups comments that share one cause into one commit, pushes the fixes,
and asks Copilot to review again when the current head has no clean review. It
repeats until the review is clean or it reaches a stop condition.

The plugin verifies the shared Agent Tasks runtime. Authentication stays in
local `gh api`; repository analysis and execution stay in GitHub Agent Tasks.

### Self Review Loop

Uses managed GitHub Agent Tasks to review the pinned pull request and fix
findings in scope. Formatting, tests, and builds stay hosted. The local
coordinator validates committed code and the live branch identity before it
imports and pushes fixes. A clean review leaves the branch unchanged.

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

Both parent agents launch their scheduler once and use a durable monitor
protocol to report every stage transition and one coalesced heartbeat per five
minutes of unchanged waiting in their own session conversation. Progress does
not depend on opening a terminal card or PR Flight.

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
local `gh api`; CI diagnosis, edits, and validation stay in GitHub Agent Tasks.

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

Each member gets five charged iterations. PR Pipeline does not reset this
budget between passes. Every accepted push records a machine-readable
checkpoint. Install `pr-conflict-resolver@trask-plugins` to use native-stack
mode; the loop checks for it before it edits or pushes any stack member.

### Historical PR Audit

Uses one managed GitHub Agent Task to audit a merged pull request against its
pinned historical base, head, diff, and discussion. The worker compares changed
areas with sibling implementations, fixes every validated finding, and runs the
historical tree's formatting, tests, and builds. The local coordinator validates
the task's commit history, report, receipt, and live repository identity before
it imports and pushes the fixes to `trask-pr-audit-<number>`.

It also compares each changed area with the closest sibling implementations in
that historical tree, and treats an unexplained departure from a strong,
directly applicable precedent as a finding worth raising.

The merged pull request never changes. The audit branch is the only thing this
agent pushes, and a first pass that finds nothing pushes no branch at all.

The plugin locates and verifies the shared `cloud_task.py`, then runs it with
policy `marketplace-agent-worker@1`. The backend is GitHub Agent Tasks through
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
