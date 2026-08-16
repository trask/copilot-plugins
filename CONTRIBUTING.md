# Contributing

Fork the repository, create a branch for one change, and open a pull request
against `main`.

Run the whole test suite before you submit:

```bash
python -m pytest
```

Keep each plugin complete under `plugins/<name>/`, including its agent
definitions, scripts, and tests. Bump the plugin version in both `plugin.json`
and `.github/plugin/marketplace.json` when you publish a change to behavior.

Shared code and documentation must not contain credentials, employer details,
internal hostnames, or personal filesystem paths.
