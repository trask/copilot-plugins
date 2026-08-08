# Contributing

Fork the repository, create a focused branch, and open a pull request against
`main`.

Run the full test suite before submitting:

```bash
python -m pytest
```

Keep each plugin self-contained under `plugins/<name>/`, including its agent
definitions, scripts, and tests. Bump the plugin version in both `plugin.json`
and `.github/plugin/marketplace.json` when publishing a behavior change.

Shared code and documentation must not contain credentials, employer details,
internal hostnames, or personal filesystem paths.
