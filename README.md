# copilot-plugins

GitHub Copilot CLI plugins that help you review pull requests.

## Install

Add the marketplace once:

```bash
copilot plugin marketplace add trask/copilot-plugins
```

Then install any of the plugins:

```bash
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

## Plugins

### PR Reviewer

Uses a managed GitHub Agent Task to inspect the complete pinned pull request
diff and run focused probes in isolation. The local coordinator validates the
task's committed report and receipt, then checks each candidate with a separate
Claude evaluator before it creates and verifies one pending review.

Run this agent with GPT-5.6 Sol at high reasoning effort. It checks each finding
with a separate Claude Sonnet 5 evaluator. It requires the managed `cloud`
skill from copilot-config commit
`e67d61da91c514eeea12179997aa4f35d3d737da` and policy
`marketplace-agent-worker@1`. It never uses Cloud Sandboxes or a local-analysis
fallback.

### Copilot Review Loop

Works through the Copilot pull request review comments that nobody has resolved
yet. It groups comments that share one cause into one commit, pushes the fixes,
and asks Copilot to review again when the current head has no clean review. It
repeats until the review is clean or it reaches a stop condition.

### Self Review Loop

Uses one managed GitHub Agent Task to review the pinned pull request, fix every
validated finding in scope, and run the repository's formatting, tests, and
builds. The local coordinator validates the task's commit history, report,
receipt, and live branch identity before it imports and pushes the fix commits.
A clean review leaves the branch unchanged.

This plugin requires the managed `cloud` skill from copilot-config commit
`e67d61da91c514eeea12179997aa4f35d3d737da`. It runs `cloud_task.py` with policy
`marketplace-agent-worker@1`. The backend is GitHub Agent Tasks through local
`gh api`; there is no Cloud Sandbox, custom agent, local repository analysis,
or local execution fallback.

### PR Description

Uses a managed GitHub Agent Task to review the current pull request title and
description against the complete diff. The local coordinator pins the pull
request and permission context, validates the task's committed report and
receipt, then keeps ideal text or applies the proposed replacement through
GitHub's authenticated API.

This plugin requires the managed `cloud` skill from copilot-config commit
`e67d61da91c514eeea12179997aa4f35d3d737da`. It runs `cloud_task.py` with policy
`marketplace-agent-worker@1`. The backend is GitHub Agent Tasks through local
`gh api`; there is no Cloud Sandbox, custom agent, or local-analysis fallback.

### PR Pipeline

Runs conflict handling, Copilot review, self review, CI repair, and description
validation for one pull request. The same plugin includes PR Stack Pipeline,
which applies those existing agents to a selected suffix of a native GitHub
stack with at most two passes.

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

### PR Conflict Resolver

Resolves the merge conflicts on a pull request in one pass. It reads the history
behind each conflicted file first, then keeps what both sides meant to do rather
than picking a side, and records why in the merge commit. It stops and reports
when the two sides genuinely contradict each other. It refuses to rewrite an
ordinary branch with dependents. For a native GitHub stack, it rebases every
descendant in a throwaway clone and publishes the complete stack with one
atomic, exact-lease push. A run that publishes and then still reads as
conflicting is finished rather than failed, and a caller that wants another
integration starts another run. It never posts anything to GitHub. Its
machine-facing descendant propagation operation uses the same topology checks and
atomic publisher after a lower stack member receives a CI fix.

### CI Fix Loop

Fixes only failures attributable to the pull request. A standalone run on a
native GitHub stack checks CI from the bottom member through the top, starts real
repair work at the lowest failure, and uses PR Conflict Resolver to propagate
each fixed head through its descendants before their checks run. It does not run
the review, description, or other PR Pipeline stages. A pull request outside a
native stack keeps the single-PR behavior.

Each member gets five charged iterations. A PR Pipeline run keeps its existing
five charged iterations per outer pass and absolute ten across two passes. Every
accepted push records a durable machine-readable checkpoint. Install
`pr-conflict-resolver@trask-plugins` to use native-stack mode; the loop checks
for it before it edits or pushes any stack member.

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

This plugin requires the managed `cloud` skill from copilot-config commit
`e67d61da91c514eeea12179997aa4f35d3d737da`. It runs `cloud_task.py` with policy
`marketplace-agent-worker@1`. The backend is GitHub Agent Tasks through local
`gh api`; there is no Cloud Sandbox, custom agent, local repository analysis,
or local execution fallback.

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
