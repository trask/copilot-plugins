# copilot-plugins

GitHub Copilot CLI plugins for pull request review workflows.

## Install

Register the marketplace once:

```bash
copilot plugin marketplace add trask/copilot-plugins
```

Then install either plugin:

```bash
copilot plugin install draft-pr-review@trask-plugins
copilot plugin install iterate-with-copilot-review@trask-plugins
```

Restart Copilot after installing or updating a plugin.

## Plugins

### Draft PR Review

Reviews the authoritative GitHub pull request diff, independently evaluates
each candidate finding, and creates a verified pending review containing only
high-confidence inline comments.

This agent must run on a Claude model because it uses GPT-5.6 Sol as an
independent evaluator.

### Iterate with Copilot Review

Processes unresolved Copilot pull request review comments, groups related
feedback into coherent commits, publishes fixes, requests another Copilot
review, and repeats until the review is clean or a stop condition is reached.

## Update

```bash
copilot plugin marketplace update trask-plugins
copilot plugin update draft-pr-review
copilot plugin update iterate-with-copilot-review
```

## Requirements

- GitHub Copilot CLI
- GitHub CLI (`gh`), authenticated for the repositories being reviewed
- Python 3.10 or newer

## License

[MIT](LICENSE)
