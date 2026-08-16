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
copilot plugin install pr-description-loop@trask-plugins
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

### PR Description Loop

Shows the current pull request title and description. It validates them
unchanged when you approve them as they are. Otherwise it works with you on a
replacement and applies it only after you approve that exact text. It checks
every accepted outcome against the pinned pull request head and the exact live
text.

### Optional PR Flight State Sharing

Self Review Loop and PR Description Loop can copy the few completion facts that
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
copilot plugin update pr-description-loop
```

## Requirements

- GitHub Copilot CLI
- GitHub CLI (`gh`), signed in for the repositories you review
- Python 3.10 or newer

## License

[MIT](LICENSE)
