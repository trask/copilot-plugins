# Contributing

Fork the repository, create a branch for one change, and open a pull request
against `main`.

Run the fast test pyramid before you submit:

```bash
python tools/validate.py fast
```

The command runs ordinary tests and retained Git sentinels in parallel, then
runs Windows kernel process tests serially. CI and non-Windows machines keep
four workers with load distribution. Local Windows machines with at least 16
logical processors use eight workers with work stealing.
`python tools/validate.py full` runs the same complete suite.

Keep each plugin complete under `plugins/<name>/`, including its agent
definitions, scripts, and tests. Bump the plugin version in both `plugin.json`
and `.github/plugin/marketplace.json` when you publish a change to behavior.

Shared code and documentation must not contain credentials, employer details,
internal hostnames, or personal filesystem paths.
