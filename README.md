# copilot-config

Portable GitHub Copilot configuration, shared across machines.

## Setup on a new machine

```bash
git clone <repo-url> /c/src/copilot-config
cd /c/src/copilot-config
./link.sh
```

`link.sh` creates links from `~/.copilot` into this repo, so edits made through
Copilot land directly in the working tree and show up in `git status`. On Windows
(Git Bash / MSYS) it uses NTFS junctions rather than symlinks, because symlinks
require administrator rights unless Developer Mode is enabled. On macOS and Linux
it uses ordinary symlinks. The script is idempotent — re-run it any time.

Anything already at a target path is moved aside to
`~/.copilot-backups/<timestamp>/` first — deliberately outside `~/.copilot`,
since a leftover copy under `skills/` or `agents/` would be scanned and loaded as
a duplicate.

## What is tracked

| Path | Purpose |
| --- | --- |
| `copilot-instructions.md` | Global custom instructions (copied, not linked) |
| `settings.json` | Theme, allowed URLs, marketplaces, enabled plugins (copied) |
| `instructions/` | Conditional `*.instructions.md` rules (linked) |
| `agents/` | Custom agents and their scripts (linked) |
| `skills/*` | Hand-written skills only (linked per skill) |

Bundled skills that ship with Copilot (`docx`, `pptx`, `xlsx`, `excalidraw`,
`loop`, `expense-report`, `web-artifacts-builder`, …) are deliberately excluded —
they reinstall themselves and would only create merge noise.

Plugins are not vendored either. `settings.json` records the marketplace and the
enabled plugin list, and Copilot reinstalls them into
`~/.copilot/installed-plugins/` on first launch.

## What is deliberately not tracked

* `config.json` — machine-managed: trusted folders, absolute plugin cache paths
* `permissions-config.json` — per-machine absolute repository paths
* `data.db`, `session-store.db`, `chats/`, `session-state/`, `logs/` — local state
* `m-encryption-key.enc`, `m-sync-state.json` — secrets and device identity

## Updating

Files under a link are edited in place, so just commit them:

```bash
git add -A
git commit -m "Update instructions"
git push
```

The two copied files (`copilot-instructions.md`, `settings.json`) need an
explicit pull back into the repo after Copilot changes them:

```bash
./link.sh pull
```

On the other machine, `git pull` then re-run `./link.sh` to refresh the copied
files.
