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
copilot plugin install conflict-fix-loop@trask-plugins
copilot plugin install historical-pr-audit@trask-plugins
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

### Conflict Fix Loop

Resolves the merge conflicts on a pull request and repeats until GitHub reports
it mergeable. It reads the history behind each conflicted file first, then keeps
what both sides meant to do rather than picking a side, and records why in the
merge commit. It stops and reports when the two sides genuinely contradict each
other. It refuses to rewrite an ordinary branch with dependents. For a native
GitHub stack, it rebases every descendant in a throwaway clone and publishes the
complete stack with one atomic, exact-lease push. It never posts anything to
GitHub.

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
copilot plugin update conflict-fix-loop
copilot plugin update historical-pr-audit
```

## Requirements

- GitHub Copilot CLI
- GitHub CLI (`gh`), signed in for the repositories you review
- Python 3.10 or newer

## License

[MIT](LICENSE)
