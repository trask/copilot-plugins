# Contributing

Fork the repository, create a branch for one change, and open a pull request
against `main`.

Run the fast test pyramid before you submit:

```bash
python tools/validate.py fast
```

Use `tools/validate.py` rather than `python -m pytest` for the full test pyramid:
it runs ordinary tests and retained Git sentinels in parallel, then runs
Windows kernel process tests serially. CI and non-Windows machines use four
workers with load distribution. Local Windows machines with at least 16
logical processors use eight workers with work stealing. Pytest progress and
failures appear in the terminal and CI logs.
`python tools/validate.py full` runs the same complete suite.

To run one file or node ID locally without starting workers:

```bash
python tools/validate.py test tests/test_test_pyramid.py
```

The `test` command accepts more pytest selectors and options after the path.

Keep each plugin complete under `plugins/<name>/`, including its agent
definitions, scripts, and tests. Bump the plugin version in both `plugin.json`
and `.github/plugin/marketplace.json` when you publish a change to behavior.

Shared code and documentation must not contain credentials, employer details,
internal hostnames, or personal filesystem paths.
