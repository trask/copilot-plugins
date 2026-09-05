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
copilot plugin install orchestration-agents@trask-plugins
```

Restart Copilot after you install or update a plugin.

## Plugins

### PR Reviewer

Reads the pull request diff that GitHub reports, and checks each possible
finding with its own separate evaluator. It then creates a pending review that
holds only the findings it can confirm, and verifies that every inline comment
points at a real line of that diff.

Run this agent on a Claude model. It checks its own findings with GPT-5.6 Sol,
and that check only works when the evaluator comes from another model family.

### Copilot Review Loop

Works through the Copilot pull request review comments that nobody has resolved
yet. It groups comments that share one cause into one commit, pushes the fixes,
and asks Copilot to review again when the current head has no clean review. It
repeats until the review is clean or it reaches a stop condition.

### Self Review Loop

Reads the pull request diff that GitHub reports and checks each possible
finding with its own separate evaluator. It then commits the fixes instead of
posting review comments. Each commit records the finding, the analysis, and the
upsides and downsides, so you can read the reasoning in git. It pushes after
every pass and reviews the new head again, until a whole pass finds nothing or
it reaches a stop condition.

Run this agent on a Claude model. It checks its own findings with GPT-5.6 Sol,
and that check only works when the evaluator comes from another model family.

### PR Description

Reviews the current pull request title and description against the diff. It
validates ideal text unchanged or automatically applies a better title and
description. It checks every outcome against the pinned pull request head and
the exact live text.

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

Audits a pull request that already merged, without changing it. It pins the
exact base and head commits that pull request merged from, captures the diff and
the discussion GitHub reported for that snapshot, and moves a fresh session
branch to the historical head so the code around it is the code the author
wrote. It checks each possible finding with its own separate evaluator, commits
the fixes on a branch named `trask-pr-audit-<number>`, and audits the new head
again until a whole pass finds nothing.

It also compares each changed area with the closest sibling implementations in
that historical tree, and treats an unexplained departure from a strong,
directly applicable precedent as a finding worth raising.

The merged pull request never changes. The audit branch is the only thing this
agent pushes, and a first pass that finds nothing pushes no branch at all.

Run this agent on a Claude model. It checks its own findings with GPT-5.6 Sol,
and that check only works when the evaluator comes from another model family.

### Orchestration Agents

Select **Astra Coordinator** in the agent dropdown and describe the repository
task, pull request, or stack you want coordinated. Astra plans the work and
delegates implementation and validation to **Luna Implementer** children, which
run with the `gpt-5.6-luna` model and high reasoning effort. Astra does not edit
the coordinator worktree.

When starting a child through an app-native session kickoff, use the
plugin-qualified agent ID `orchestration-agents:luna-implementer`.

Each child reports its authoritative runtime model and evidence once. Astra
confirms the effective model from recorded usage before authorizing work, and
does not repeat the gate for an unchanged authorized runtime. A restart,
recovery, runtime identity change, or mismatch requires a new gate; unavailable
reasoning-effort/profile metadata is a disclosed limitation, not another gate.

Every child has one active assignment with a distinguishable ID, repository and
branch ownership, expected head, acceptance criteria, publication boundary, and
named dependencies. The coordinator keeps the small durable record with the
existing session SQL or todo tools. Terminal child reports are exactly `DONE`,
`BLOCKED`, or `READY`; ordinary implementation ends in `DONE`, while `READY`
is reserved for an explicit preparation gate. Idle events without a result are
reconciled against the current assignment, given at most one bounded recovery,
then surfaced as blocked if the outcome remains unknown.

Handoffs keep `stack_number`, `pr_number`, `repo`, `branch_ref`, and
`expected_head` separate. A stack number is not a PR number: use the native
stack endpoint for `stack_number` and the pull request endpoint for
`pr_number`. The agents do not widen permissions, escalate models
automatically, or use a restrictive tools allowlist.

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
copilot plugin update orchestration-agents
```

## Requirements

- GitHub Copilot CLI
- GitHub CLI (`gh`), signed in for the repositories you review
- Python 3.10 or newer

## License

[MIT](LICENSE)
