# copilot-plugins

Public GitHub Copilot CLI plugins. The `trask-plugins` marketplace distributes
them.

Commit your work and push it straight to `main`. Do not open a pull request for
it, and do not ask first. This covers every change, including a change that
touches only tests or only documentation. It applies whether or not the change
publishes a new plugin version. Only a contributor without write access uses a
pull request. See [Publication](#publication) for the rest of the flow.

## Structure

- `.github/plugin/marketplace.json` is the marketplace catalog.
- Each `plugins/<name>/` directory holds one complete plugin.
- Keep agent definitions, scripts, and tests inside their plugin directory.
- A plugin's version in `plugin.json` and the marketplace entry must match.

## Validation

1. Run the narrowest tests that cover the change. Run `python -m pytest` only
   for shared repository infrastructure, marketplace-wide changes, or when
   targeted tests expose a cross-plugin concern. CI runs the full suite after
   every push.
2. Run `git diff --check`, read `git status`, and review the final diff.
3. To check a plugin change, install the plugin from the marketplace. That
   requires publishing it first. The Copilot CLI cannot install from a local
   directory, so never try one. `plugin install` accepts only
   `plugin@marketplace`, `owner/repo`, `owner/repo:path`, or a URL.

## Publication

The marketplace catalog comes from `main`, so nobody can install or check a
change until you push it there.

Bump the plugin version in both `plugin.json` and the marketplace entry when the
change alters behavior or packaging. A change that touches only tests or only
documentation keeps the current version.

Publishing a behavior change is part of the normal flow, not a separate request:

1. Rebase onto `origin/main`, which moves on its own while you work.
2. Bump the version in both manifests when the change requires it.
3. Run the validation steps above.
4. Commit and push to `main`.
5. Refresh the catalog with `copilot plugin marketplace update trask-plugins`.
6. Install each changed plugin with `copilot plugin install <name>@trask-plugins`.
7. Confirm the installed versions with `copilot plugin list`.

Skip steps 5 through 7 when the change cannot alter how an installed plugin
behaves, such as a change to this repository's documentation or to its tests.
