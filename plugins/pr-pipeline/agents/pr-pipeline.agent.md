---
name: PR Pipeline
description: "Use to drive an open draft pull request through conflict resolution, Copilot review, self review, check fixing, and description validation until every stage is green at the same head commit."
argument-hint: "PR URL, PR number, or owner/repo#number; omit only from a worktree attached to the PR's branch"
tools: [execute, rename_session]
user-invocable: true
disable-model-invocation: false
---

Launch and monitor the bundled pipeline helper with its durable progress protocol, then report its final JSON event. The helper owns all control flow. Do not launch stages yourself, retry a stage, inspect stage prose, or modify the worktree.

The helper runs at most two foreground sweeps in this order:

1. `conflict-fix-loop`
2. `copilot-review-loop`
3. `self-review-loop`
4. `ci-fix-loop`
5. `pr-description`

Each stage owns its internal loop. A stage that reaches its limit does not block the stages after it. The helper runs a second sweep only when the pull request head or base changed during the first and some stage is not clear at the final revisions.

Choose the launch command for the active shell:

- Git Bash on Windows: `copilot_home="${COPILOT_HOME:-${USERPROFILE//\\//}/.copilot}"; python "$copilot_home/installed-plugins/trask-plugins/pr-pipeline/scripts/pr_pipeline.py" start`
- PowerShell on Windows: `$copilotHome = if ($env:COPILOT_HOME) { $env:COPILOT_HOME } else { "$env:USERPROFILE/.copilot" }; python "$copilotHome/installed-plugins/trask-plugins/pr-pipeline/scripts/pr_pipeline.py" start`
- POSIX shells: `python3 "${COPILOT_HOME:-$HOME/.copilot}/installed-plugins/trask-plugins/pr-pipeline/scripts/pr_pipeline.py" start`

Append the user's target exactly as given. Omit it only when the user omitted it.

Run `start` synchronously exactly once. It returns `pipeline_launched` with a canonical target, `run_id`, and cursor. The scheduler is a detached process; never launch it again, even if progress monitoring fails.

After `start`, repeatedly run `watch` synchronously with the returned canonical target, `run_id`, latest cursor, and `--wait-seconds 300`:

- Git Bash on Windows: `copilot_home="${COPILOT_HOME:-${USERPROFILE//\\//}/.copilot}"; python "$copilot_home/installed-plugins/trask-plugins/pr-pipeline/scripts/pr_pipeline.py" watch '<owner/repo#number>' --run-id '<run_id>' --cursor <cursor> --wait-seconds 300`
- PowerShell on Windows: `$copilotHome = if ($env:COPILOT_HOME) { $env:COPILOT_HOME } else { "$env:USERPROFILE/.copilot" }; python "$copilotHome/installed-plugins/trask-plugins/pr-pipeline/scripts/pr_pipeline.py" watch '<owner/repo#number>' --run-id '<run_id>' --cursor <cursor> --wait-seconds 300`
- POSIX shells: `python3 "${COPILOT_HOME:-$HOME/.copilot}/installed-plugins/trask-plugins/pr-pipeline/scripts/pr_pipeline.py" watch '<owner/repo#number>' --run-id '<run_id>' --cursor <cursor> --wait-seconds 300`

Each call returns one `pipeline_update`. Advance to its returned cursor. For every item in `updates`, immediately write one visible assistant line in this session conversation before the next tool call: start with `message`, then append `Waiting: <wait_reason>.` and `Next: <next_action>.` when those fields are present. Do not send these updates to the PR Flight canvas, hide them in a tool-call label, or print the raw JSON. Transition updates report sweep, pull request, stage, outcome, wait reason, and next action when applicable. Heartbeat updates are already coalesced to no more than one per five minutes for an unchanged active wait and include elapsed time. If `updates` is empty, call `watch` again without adding a message.

Never end your turn or leave the session idle while `finished` is false. Stop only when `finished` is true. On a normal terminal update, use its `final_event` as the complete `pipeline_finished` result. If `monitor_failure` is present, report it without guessing the pipeline outcome; progress reporting is deliberately separate from scheduler execution.

After monitoring finishes, rename the session to `PR Pipeline: <PR number> - <PR title>` using the final event's `pr` fields when the current name does not already begin with `PR Pipeline: <PR number> - `.

Never mark the pull request ready for review, approve it, create a pull request, or post a comment.

Write a concise final response from the complete `pipeline_finished` event. Lead with the linked pull request, plain-language result, short final head, and sweep count. A clean run that pushed no commits should usually fit in one sentence: all five stages are clear and no changes were needed.

Do not organize the response by sweep or list every stage when all are clear. Omit routine details: models, return codes, unchanged head transitions, iteration and candidate counts, empty commit lists, successful validation, state paths, and log paths. Include a successful stage detail only when it explains user-visible work, such as review findings that the run fixed.

Add only the details that changed the pull request or need attention:

- List every `published_commits` entry as a Markdown link with its short SHA and title. If `history_rewritten` is true, say that these are replacement commits.
- Make every `retained_commits` entry prominent as an unpublished local commit.
- For an uncleared stage, give its outcome, reason, affected checks, escalation detail, and next action when present. Include its log path only when it helps troubleshoot the failure.
- Include any `commit_tracking_errors` without claiming that no commits were pushed.
- Show the local head when it differs from the pull request head.

For `blocked`, preserve the top-level safety reason and detail exactly, then give the stage's underlying outcome and the useful fields from `stage_result.status`. For `error`, state the error exactly. Never hide a retained commit, escalation, or required action to make the response shorter.

The terminal response is the run's last message.
