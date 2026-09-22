# Contributing

Fork the repository, create a branch for one change, and open a pull request
against `main`.

Run the fast test pyramid before you submit:

```bash
python tools/validate.py fast
```

The command runs ordinary tests and retained Git sentinels with four workers,
then runs Windows kernel process tests serially. `python tools/validate.py full`
runs the same complete suite. The `legacy` mode selects the remaining integration
families for test-pyramid equivalence audits; it is not an additional CI lane.

Keep each plugin complete under `plugins/<name>/`, including its agent
definitions, scripts, and tests. Bump the plugin version in both `plugin.json`
and `.github/plugin/marketplace.json` when you publish a change to behavior.

Shared code and documentation must not contain credentials, employer details,
internal hostnames, or personal filesystem paths.
