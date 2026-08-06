---
name: copilot-config-maintenance
description: "Use when the user explicitly asks to add, update, remove, or manage their Copilot skills, custom agents, instructions, settings, plugins, or the copilot-config repository. Do not use for ordinary use of those existing capabilities."
argument-hint: "Requested Copilot configuration change"
---

# Copilot Config Maintenance

Use this skill only for an explicit request to change or manage the user's
Copilot configuration. Do not trigger merely because the user asks to use an
existing skill, agent, instruction, setting, or plugin.

## Source of Truth and Workspace

- The canonical repository is `C:\src\copilot-config`, hosted at
  `trask/copilot-config` on GitHub and configured as a Copilot project.
- The repository is the source of truth. Never directly maintain a duplicate
  skill, agent, instruction, or setting under `~/.copilot`.
- In general chat, never edit the primary checkout. Create or use an isolated
  project session for code and configuration changes.
- Inside a project session, work in that session's current worktree. Do not
  hardcode `C:\src\copilot-config` as the edit target.
- Inspect `git status` before editing and preserve unrelated changes. Also
  inspect the relevant existing configuration, `README.md`, `link.sh`, remotes,
  tracking branch, default branch, and repository conventions.

## Installation Model

- `instructions/` and `agents/` are linked as complete directories.
  Adding, removing, or renaming files below them does not require a `link.sh`
  list change.
- `skills/` is not linked wholesale. Every hand-written `skills/<name>`
  directory must be listed separately in `linked_dirs` in `link.sh`. Add,
  remove, or rename that entry whenever the skill directory changes.
- `copilot-instructions.md` and `settings.json` are copied rather than linked.
  Normal repository edits are installed into `~/.copilot` with `./link.sh`.
  Use `./link.sh pull` only to intentionally import changes first made under
  `~/.copilot`.
- Never run `./link.sh` from an isolated worktree: it would point
  `~/.copilot` at a disposable worktree. Run it only from the primary clone
  when intentionally installing local configuration.
- The linker puts displaced files under `~/.copilot-backups/`, outside
  `~/.copilot`, so Copilot cannot discover duplicate configuration. Restart
  Copilot after installation or whenever discovery/reload is needed.

## Change and Validation Workflow

1. Make the smallest complete repository change, including documentation and
   the `linked_dirs` entry when the skill set changes.
2. For a skill or agent, verify its YAML frontmatter, trigger behavior, paths,
   and any relevant focused tests. A maintenance skill's trigger must distinguish
   configuration changes from ordinary use of the capability.
3. Run the repository's relevant tests, including tests coupled to inherited
   branch changes. Check `link.sh` with the available shell syntax check when it
   changes.
4. Run `git diff --check`, inspect `git status`, and review the final diff.
   Do not include or overwrite unrelated work.

## Publish

After validation:

1. Commit all intended changes with the repository- and session-required
   trailers.
2. Push the current feature branch to its existing upstream. If it has none,
   follow the configured policy to push to the user's fork.
3. Open or update a **draft** pull request into `main` using the normal GitHub
   remote, and always request a Copilot review. Do not push directly to `main`
   unless the user explicitly requested a direct-main workflow.
4. Verify the draft state and review request, then provide the PR link.

For this personal configuration repository, work is not complete until it is
published to GitHub so another machine can sync after merge. Do not stop with
uncommitted changes in an isolated worktree.

## After Merge

In the primary clone, pull `main`. Run `./link.sh` when a skill was added,
renamed, or removed, or when a copied file needs installation, then restart
Copilot. Existing linked agents and instructions update without rerunning
`./link.sh`, but restart when needed for Copilot to discover or reload them.
