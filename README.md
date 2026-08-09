# copilot-plugins

GitHub Copilot CLI plugins for pull request review workflows.

## Install

Register the marketplace once:

```bash
copilot plugin marketplace add trask/copilot-plugins
```

Then install either plugin:

```bash
copilot plugin install pr-reviewer@trask-plugins
copilot plugin install copilot-review-loop@trask-plugins
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

## Update

```bash
copilot plugin marketplace update trask-plugins
copilot plugin update pr-reviewer
copilot plugin update copilot-review-loop
```

## Requirements

- GitHub Copilot CLI
- GitHub CLI (`gh`), authenticated for the repositories being reviewed
- Python 3.10 or newer

## License

[MIT](LICENSE)
