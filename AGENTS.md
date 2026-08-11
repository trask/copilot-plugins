# copilot-plugins

Public GitHub Copilot CLI plugins distributed through the `trask-plugins`
marketplace.

Commit and push your work directly to `main`. Do not open a pull request for it,
and do not ask for confirmation first. This covers every change, including a
test-only or documentation-only one, and applies whether or not the change is
part of publishing a new plugin version. Only a contributor without write access
uses a pull request. See [Publication](#publication) for the rest of the flow.

## Structure

- `.github/plugin/marketplace.json` is the marketplace catalog.
- Each `plugins/<name>/` directory is a self-contained plugin.
- Keep agent definitions, scripts, and tests inside their plugin directory.
- A plugin's version in `plugin.json` and the marketplace entry must match.

## Validation

1. Run the narrowest affected tests, then `python -m pytest`.
2. Run `git diff --check`, inspect `git status`, and review the final diff.
3. Verify a plugin change by installing it from the marketplace, which requires
   publishing it first. The Copilot CLI cannot install from a local directory,
   so never attempt one; `plugin install` accepts only `plugin@marketplace`,
   `owner/repo`, `owner/repo:path`, or a URL.

## Publication

The marketplace catalog is served from `main`, so a change cannot be installed
or verified until it is pushed there.

Bump the plugin version in both `plugin.json` and the marketplace entry for a
behavior or packaging change. Test-only and documentation-only changes keep the
current version.

Publishing a behavior change is part of the normal flow rather than a separate
request:

1. Rebase onto `origin/main`, which moves independently of local work.
2. Bump the version in both manifests when the change requires it.
3. Run the validation steps above.
4. Commit and push to `main`.
5. Refresh the catalog with `copilot plugin marketplace update trask-plugins`.
6. Install each changed plugin with `copilot plugin install <name>@trask-plugins`.
7. Confirm the installed versions with `copilot plugin list`.

Skip steps 5 through 7 when the change cannot affect how an installed plugin
behaves, such as a repository documentation change or a test-only change.
