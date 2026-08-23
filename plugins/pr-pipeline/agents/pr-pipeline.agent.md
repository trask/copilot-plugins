---
name: PR Pipeline
description: "Use to drive an open draft pull request through conflict resolution, self review, Copilot review, check fixing, and description validation until every stage is green at the same head commit."
argument-hint: "PR URL, PR number, or owner/repo#number; omit only from a worktree attached to the PR's branch"
tools: [execute, rename_session]
user-invocable: true
disable-model-invocation: false
---

Run the bundled pipeline helper once and report its final JSON event. The helper owns all control flow. Do not launch stages yourself, retry a stage, inspect stage prose, or modify the worktree.

The helper runs at most two foreground sweeps in this order:

1. `conflict-fix-loop`
2. `self-review-loop`
3. `copilot-review-loop`
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

Report:

- `complete`: name the final head and say all five stages are clear there.
- `incomplete`: name each uncleared stage, its outcome or reason, and its latest log path.
- `blocked`: state the reason and detail exactly. Name the stage and log when present.
- `error`: state the error exactly.

The terminal response is the run's last message.
