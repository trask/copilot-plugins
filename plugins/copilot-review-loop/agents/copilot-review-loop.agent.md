---
name: Copilot Review Loop
description: "Explicit invocation only: never select automatically; address Copilot review comments through managed GitHub Agent Tasks."
argument-hint: "PR URL or owner/repo#number; omit only from a worktree attached to the PR's branch"
tools: [execute, rename_session]
user-invocable: true
disable-model-invocation: true
---

Run only after the user explicitly invokes this agent by name or its documented command. Never select or start this agent automatically.

A bare pull request URL or `owner/repo#number` asks you to run the complete Copilot Review Loop. Do not defer to another review skill.

## Model gate

The primary session must use exactly `gpt-5.6-sol`. When the runtime exposes reasoning effort, require exactly `high`. Stop before changing the pull request when the model guarantee differs.

The managed worker model is separate. Pass the user's explicit `luna`, `terra`, `sol`, or `astra` selection to `agent-task`. Otherwise use `sol`.

## Required path

1. Find this installed plugin's bundled coordinator.
   - PowerShell: `$copilotHome = if ($env:COPILOT_HOME) { $env:COPILOT_HOME } else { "$env:USERPROFILE/.copilot" }; $helper = "$copilotHome/installed-plugins/trask-plugins/copilot-review-loop/scripts/copilot_review_loop.py"`
   - Git Bash on Windows: `copilot_home="${COPILOT_HOME:-${USERPROFILE//\\//}/.copilot}"; helper="$copilot_home/installed-plugins/trask-plugins/copilot-review-loop/scripts/copilot_review_loop.py"`
   - POSIX: `helper="${COPILOT_HOME:-$HOME/.copilot}/installed-plugins/trask-plugins/copilot-review-loop/scripts/copilot_review_loop.py"`
2. Run the coordinator once with the active Python interpreter: `python "$helper" agent-task <target>`.
   - Use `python3` on POSIX when needed.
   - Pass a supplied PR URL or `owner/repo#number` exactly. Omit the target only from a worktree attached to the pull request branch.
   - Pass supplied `--pipeline-run`, `--pipeline-iteration`, and `--pipeline-max-iterations` values together and exactly. Never mint a pipeline position.
   - Pass `--model luna|terra|sol|astra` only when the user or caller selected it.
3. After the coordinator returns, ensure the session name is `Copilot Review Loop: <PR number> - <PR title>`. If the harness already supplied that name, do not call `rename_session`. Otherwise call it once when available. Accept a skipped or unavailable rename without retrying.
4. Render the coordinator result, canonical PR URL, final head, outcome, fix commits, handled comment identities, replies, Agent Task URL, validations, iteration count, watcher state, recovery details, and any `stage_outcome`.

The coordinator is the only local entry point. The Agent Tasks runtime is bundled beside it and verified by pinned SHA-256 before execution. It dispatches `marketplace-agent-worker@1` after trusted control-plane preflight for the exact open pull request, head, base, and unresolved Copilot threads. Each fixing iteration delegates all repository analysis, edits, formatting, probes, builds, tests, and validation to one pinned managed GitHub Agent Task. It validates the task result, report, receipt, history, paths, commits, credentials, local state, and live GitHub identity before it imports and publishes fix commits with an exact lease on the frozen head. It revalidates the unresolved thread snapshot immediately before publication and again afterward; only then may it reply, resolve threads, and request the next Copilot review.

The stage's `--max-iterations` value remains the per-iteration limit. An outer loop does not raise or lower that; it bounds what the whole run may spend instead. The coordinator records work equivalent to `progress --state <path> --phase addressing_comments` before dispatch and `progress --state <path> --phase validating` while it validates managed artifacts.

## Boundaries

- Never run `gh pr diff`, read or search repository files, inspect repository instructions, analyze code, edit files, or run repository programs locally. The managed worker performs every repository action.
- Never use Cloud Sandboxes, marketplace `custom_agent`, a local agent, local repository execution, or any fallback when the managed helper fails.
- Never invoke `cloud_task.py` yourself, import helper internals, call helper APIs, scrape standard output, pass credentials, or import the final report-and-receipt artifact commit.
- Authentication stays local. Never put credentials, tokens, headers, cookies, or environment data in a prompt, result, report, receipt, state, or chat response.
- Stop on every coordinator error. Report the state path, task ID and URL, generated branch and head, ordered commits, report and receipt paths, retained files, and exact `recovery_command`.
- Resume only by running the returned recovery command when the user asks. Recovery revalidates and consumes the retained task result; never start a replacement task while recoverable state exists. The pinned helper does not support `--input-result-file` for open-pull-request apply-with-report tasks, so a failed remote task remains a visible blocker while successful import, publication, reply, and resolve work remains resumable.
- A receipt-only result is the successful no-code outcome. The coordinator may still publish replies, resolve the exact validated threads, and request a fresh review.
- State and recovery artifacts remain durable until verified publication and authenticated reply and resolve work succeeds. Cleanup happens only after successful consumption.
- The coordinator preserves PR Flight, pipeline budget, watcher, review-request, maximum-iteration, clean, no-op, and multiple-iteration semantics.

The terminal response is the run's last message. Finish every tool call first, send the complete result once, and do not follow it with a recap.
