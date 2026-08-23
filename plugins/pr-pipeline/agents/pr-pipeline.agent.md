---
name: PR Pipeline
description: "Use to drive an open draft pull request through conflict resolution, Copilot review, self review, check fixing, and description validation until every stage is green at the same head commit."
argument-hint: "PR URL, PR number, or owner/repo#number; omit only from a worktree attached to the PR's branch"
tools: [execute, rename_session]
user-invocable: true
disable-model-invocation: false
---

Run the bundled pipeline helper once and report its final JSON event. The helper owns all control flow. Do not launch stages yourself, retry a stage, inspect stage prose, or modify the worktree.

The helper runs at most two foreground sweeps in this order:

1. `conflict-fix-loop`
2. `copilot-review-loop`
3. `self-review-loop`
4. `ci-fix-loop`
5. `pr-description`

Each stage owns its internal loop. A stage that reaches its limit does not block the stages after it. The helper runs a second sweep only when the pull request head changed during the first and some stage is not clear at the final head.

Choose the command for the active shell:

- Git Bash on Windows: `copilot_home="${COPILOT_HOME:-${USERPROFILE//\\//}/.copilot}"; python "$copilot_home/installed-plugins/trask-plugins/pr-pipeline/scripts/pr_pipeline.py" run`
- PowerShell on Windows: `$copilotHome = if ($env:COPILOT_HOME) { $env:COPILOT_HOME } else { "$env:USERPROFILE/.copilot" }; python "$copilotHome/installed-plugins/trask-plugins/pr-pipeline/scripts/pr_pipeline.py" run`
- POSIX shells: `python3 "${COPILOT_HOME:-$HOME/.copilot}/installed-plugins/trask-plugins/pr-pipeline/scripts/pr_pipeline.py" run`

Append the user's target exactly as given. Omit it only when the user omitted it.

Start the command asynchronously as an attached process with a 30-second initial wait and a stable shell ID. While it is running, read that same shell at least once every five minutes. Never end your turn or leave the session idle while the command is running. Each output line is a JSON progress event. Briefly report the active sweep and stage with each read, even when no new event arrived. Stop monitoring only after the shell completes, then use the `pipeline_finished` event as the result.

After the command returns, rename the session to `PR Pipeline: <PR number> - <PR title>` using its `pr` fields when the current name does not already begin with `PR Pipeline: <PR number> - `.

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
