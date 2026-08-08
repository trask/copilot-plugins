# copilot-plugins

Public GitHub Copilot CLI plugins distributed through the `trask-plugins`
marketplace.

## Structure

- `.github/plugin/marketplace.json` is the marketplace catalog.
- Each `plugins/<name>/` directory is a self-contained plugin.
- Keep agent definitions, scripts, and tests inside their plugin directory.
- A plugin's version in `plugin.json` and the marketplace entry must match.

## Validation

1. Run the narrowest affected tests, then `python -m pytest`.
2. Install changed plugins from their local directories when behavior or
   packaging changes.
3. Run `git diff --check`, inspect `git status`, and review the final diff.

## Publication

Maintainers publish directly to `main`; contributors use focused pull requests.
For a plugin release, bump its semantic version in both manifests, push the
change, and verify installation through the `trask-plugins` marketplace.
