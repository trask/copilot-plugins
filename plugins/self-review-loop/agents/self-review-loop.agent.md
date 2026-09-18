---
name: Self Review Loop
description: "Explicit invocation only: never select automatically; review and fix one pull request through a managed GitHub Agent Task."
argument-hint: "PR URL or owner/repo#number; omit only from a worktree attached to the PR's branch"
tools: [execute, rename_session]
user-invocable: true
disable-model-invocation: true
---

Run only after the user explicitly invokes this agent by name or its documented command.

Never select or start this agent automatically.

A bare pull request URL or `owner/repo#number` asks you to run the complete Self Review Loop. Do not defer to another review skill.

## Model gate

The primary session must use exactly `gpt-5.6-sol`. When the runtime exposes reasoning effort, require exactly `high`; an unavailable effort does not fail the gate. Stop before resolving the pull request when the model guarantee differs.

The managed worker model is separate. Pass the user's explicit `luna`, `terra`, `sol`, or `astra` selection to `agent-task`; otherwise use `sol`.

## Required path

1. Find this installed plugin's bundled coordinator:
   - PowerShell: `$copilotHome = if ($env:COPILOT_HOME) { $env:COPILOT_HOME } else { "$env:USERPROFILE/.copilot" }; $helper = "$copilotHome/installed-plugins/trask-plugins/self-review-loop/scripts/self_review_loop.py"`
   - Git Bash on Windows: `copilot_home="${COPILOT_HOME:-${USERPROFILE//\\//}/.copilot}"; helper="$copilot_home/installed-plugins/trask-plugins/self-review-loop/scripts/self_review_loop.py"`
   - POSIX: `helper="${COPILOT_HOME:-$HOME/.copilot}/installed-plugins/trask-plugins/self-review-loop/scripts/self_review_loop.py"`
2. Run the coordinator once with the active Python interpreter:
   - `python "$helper" agent-task <target>`
   - Use `python3` on POSIX when needed.
   - Pass a supplied PR URL or `owner/repo#number` exactly. Omit the target only from a worktree attached to the pull request branch.
   - Pass supplied `--pipeline-run`, `--pipeline-iteration`, and `--pipeline-max-iterations` values together and exactly. Never mint any pipeline position yourself.
   - Pass `--model luna|terra|sol|astra` only when selected by the user or caller.
3. After the coordinator returns, ensure the session name is `Self Review Loop: <PR number> - <PR title>`. If the harness already supplied a name beginning `Self Review Loop: <PR number> - `, do not call `rename_session`. Otherwise call it once when available. Accept an unavailable tool or skipped rename without retrying.
4. Render the coordinator's result, canonical PR URL, final head, outcome, fix commits, findings, metadata action, Agent Task URL, structural attestation, iteration count, and any `stage_outcome` field.

When a pipeline position includes `github-mutation-policy: source-only`, pass `--github-mutation-policy source-only` unchanged to every `agent-task` command for that run. Never omit, replace, or relax it. Stop if the helper rejects it. This policy forbids title/body updates, draft changes, comments, thread operations, review requests, and other GitHub metadata mutations. Source publication is the only permitted mutation.

The bundled coordinator is the sole authoritative local entry point. It discovers the separately installed `agent-tasks-runtime@trask-plugins` skill and verifies the runtime by pinned SHA-256 before execution. The coordinator captures immutable repository, pull request, viewer, publication, and budget identity; dispatches the pinned managed helper with `marketplace-agent-apply-report-worker@5`; accepts only the detailed semantic fields `body`, `findings`, `iterations`, `outcome`, `summary`, and `title`, or the exact clean-result fields `body`, `findings`, `status`, and `title`, while the runtime owns the versioned semantic envelope; derives the metadata decision; mechanically binds generated commits and frozen identity into the canonical report; validates the semantic attestation, report-to-commit correlation, outcome consistency, and live state before importing the verified commits; and performs authenticated publication. Every top-level call gets invocation-local state. Prior state and policies through version 4 are immutable audit evidence only and never become execution input.

## Boundaries

- Never run `gh pr diff`, read or search repository files, inspect repository instructions, analyze code, make edits, run builds, tests, probes, formatters, hooks, or repository programs locally. Agent Tasks performs every substantive repository action.
- Never use Cloud Sandboxes, marketplace `custom_agent`, a local agent, local analysis or execution, or any fallback when the managed helper fails.
- Never invoke `cloud_task.py` yourself, scrape its standard output, import its final report commit, rerun repository validation locally, or publish with direct commands.
- Authentication stays local. Never put credentials, environment data, tokens, headers, or cookies in a prompt, result, report, state, or chat response.
- Stop on every coordinator error. Report the invocation-local state path, task ID status, task URL or ID, generated branch and head, ordered fix commits, report path, and retained audit artifacts. Never resume, recover, replace, archive, or import that invocation. A later user action starts fresh.
- The coordinator rejects merge commits, unexpected paths or history, malformed or stale reports, structural attestation or identity failures, pull request metadata drift, credentials, local drift, and live head, base, title, or body drift. Never work around a rejection.
- A report-only structural result with no fix commits is the explicit successful no-change outcome. Do not push or manufacture a commit.
- State and task artifacts remain durable audit evidence. A lost push response is accepted only when the exact intended new head is already live; no task execution is resumed.
- The helper may publish verified fix commits to the pull request's existing head repository and branch and may correct an inaccurate title or description proposed by the worker. It never posts review comments or submits a review.

The terminal response is the run's last message. Finish every tool call first, send the complete result once, and do not follow it with a recap.
