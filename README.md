# copilot-plugins

GitHub Copilot CLI plugins for pull request review workflows.

## Install

Register the marketplace once:

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

Restart Copilot after installing or updating a plugin.

## Plugins

### PR Reviewer

Reviews the authoritative GitHub pull request diff, independently evaluates
each candidate finding, and creates a verified pending review containing only
high-confidence inline comments.

This agent must run on a Claude model because it uses GPT-5.6 Sol as an
independent evaluator.

### Copilot Review Loop

Processes unresolved Copilot pull request review comments, groups related
feedback into coherent commits, publishes fixes, requests another Copilot
review when the current head has not received a clean one, and repeats until
the review is clean or a stop condition is reached.

### Self Review Loop

Reviews the authoritative pull request diff itself, independently evaluates
every candidate finding, and then commits the fixes instead of posting review
comments. Each commit records the original finding, the analysis, and the
upsides and downsides, so the reasoning stays reviewable in git. It pushes
after every iteration and reviews the new head again until a full pass produces
no findings or a stop condition is reached.

This agent must run on a Claude model because it uses GPT-5.6 Sol as an
independent evaluator.

### PR Description Loop

Shows the current pull request title and description, validates them unchanged
when the user approves, or iterates on a focused replacement and applies it only
after explicit approval. Every accepted outcome is verified against the pinned
pull request head and exact live text.

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
- GitHub CLI (`gh`), authenticated for the repositories being reviewed
- Python 3.10 or newer

## License

[MIT](LICENSE)
