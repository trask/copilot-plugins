# copilot-config

Portable GitHub Copilot configuration, shared across machines.

## Setup on a new machine

```powershell
git clone <repo-url> C:\src\copilot-config
cd C:\src\copilot-config
.\link.ps1
```

`link.ps1` creates NTFS junctions from `~/.copilot` into this repo, so edits made
through Copilot land directly in the working tree and show up in `git status`.
Junctions are used instead of symlinks because symlinks require administrator
rights on Windows unless Developer Mode is enabled.

Anything already at a target path is moved aside to
`%USERPROFILE%\.copilot-backups\<timestamp>\` first — deliberately outside
`~/.copilot`, since a leftover copy under `skills/` or `agents/` would be scanned
and loaded as a duplicate.

## What is tracked

| Path | Purpose |
| --- | --- |
| `copilot-instructions.md` | Global custom instructions (copied, not linked) |
| `settings.json` | Theme, allowed URLs, marketplaces, enabled plugins (copied) |
| `instructions/` | Conditional `*.instructions.md` rules (junction) |
| `agents/` | Custom agents and their scripts (junction) |
| `skills/*` | Hand-written skills only (junction per skill) |

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

Files under a junction are edited in place, so just commit them:

```powershell
git add -A
git commit -m "Update instructions"
git push
```

The two copied files (`copilot-instructions.md`, `settings.json`) need an
explicit pull back into the repo after Copilot changes them:

```powershell
.\link.ps1 -Pull
```

On the other machine, `git pull` then re-run `.\link.ps1` to refresh the copied
files.
