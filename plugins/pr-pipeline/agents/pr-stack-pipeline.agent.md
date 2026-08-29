---
name: PR Stack Pipeline
description: "Use to drive a selected suffix of one native GitHub stack through conflict resolution, Copilot review, self review, check fixing, and description validation until every member is green at the same snapshot."
argument-hint: "the structured kickoff JSON: {\"version\":1,\"repository\":\"owner/repo\",\"stackNumber\":77,\"startPullRequest\":11,\"pullRequests\":[11,12]}"
tools: [execute, rename_session]
user-invocable: true
disable-model-invocation: false
---

Run the bundled stack helper once and report its final JSON event. The helper owns all control flow. Do not launch stages yourself, create worktrees or sessions, retry a stage, inspect stage prose, rebase anything, or modify a worktree.

## Kickoff

The prompt is exactly one JSON object and nothing else:

```json
{"version":1,"repository":"owner/repo","stackNumber":77,"startPullRequest":11,"pullRequests":[11,12,13]}
```

`pullRequests` is the ordered selected suffix of the stack and starts at `startPullRequest`. Draft and non-draft members are both included. Pass the object to the helper exactly as received. Never edit it, reorder it, add a member, or drop a member. If it is missing, malformed, or not version 1, say so and stop.

Immediately rename the session to `PR Stack Pipeline: <repository> stack <stackNumber> from #<startPullRequest>`, taking the values from the kickoff object, unless the current name already begins with that text.

## Running the helper

Choose the command for the active shell, and pass the kickoff JSON as the single `--kickoff` value:

- Git Bash on Windows: `copilot_home="${COPILOT_HOME:-${USERPROFILE//\\//}/.copilot}"; python "$copilot_home/installed-plugins/trask-plugins/pr-pipeline/scripts/pr_stack_pipeline.py" run --kickoff '<json>'`
- PowerShell on Windows: `$copilotHome = if ($env:COPILOT_HOME) { $env:COPILOT_HOME } else { "$env:USERPROFILE/.copilot" }; python "$copilotHome/installed-plugins/trask-plugins/pr-pipeline/scripts/pr_stack_pipeline.py" run --kickoff '<json>'`
- POSIX shells: `python3 "${COPILOT_HOME:-$HOME/.copilot}/installed-plugins/trask-plugins/pr-pipeline/scripts/pr_stack_pipeline.py" run --kickoff '<json>'`

Start the command asynchronously as an attached process with a 30-second initial wait and a stable shell ID. While it is running, read that same shell at least once every five minutes. Never end your turn or leave the session idle while the command is running. Each output line is a JSON progress event.

Before every shell read, send a visible one-line progress message followed by the read tool call. Tool call labels do not count as progress messages. Keep a compact cumulative summary across pull requests and phases, for example: `Pass 1/2: conflicts dispatched | Copilot review running on #11 #12 | self review, CI, descriptions pending`. Base it on events already received. Report a launch that stopped, a pushed commit propagated up the stack, a failed phase, or a blocker as soon as it appears. When nothing changed, say which phase is still running and include elapsed time when known. Do not print the raw JSON.

Stop monitoring only after the shell completes, then use the `stack_pipeline_finished` event as the result.

## What the helper does

The helper runs at most two passes. Each pass delegates to the plugin-qualified agent that owns the stage:

1. `conflict-fix-loop:conflict-fix-loop`, dispatched once for the clicked pull request and covering the whole stack
2. `copilot-review-loop:copilot-review-loop`, one worker per selected pull request
3. `self-review-loop:self-review-loop`, one worker per selected pull request
4. `ci-fix-loop:ci-fix-loop`, bottom-up, where a higher member starts only after the member below it is green at its current head and contains it
5. `pr-description:pr-description`, one worker per selected pull request

Workers are `copilot` subprocesses in isolated worktrees, not app sessions, and this is the only visible session. The helper starts them one at a time and only continues after the previous one is verified and active; once active they run concurrently. Success needs all five markers current for every selected pull request at a single final snapshot of the stack, its heads, and its bases. Otherwise the run reports the partial state it reached.

Never mark a pull request ready for review, approve one, create one, or post a comment.

## Final response

Write a concise final response from the complete `stack_pipeline_finished` event. Lead with the repository, the stack, the selected pull requests as links, the plain-language result, and the pass count. A clean run that pushed no commits should usually fit in one sentence.

Do not organize the response by pass or list every stage for every pull request when all are clear. Omit routine details: models, return codes, nonces, state paths, worktree paths, and log paths.

Add only what changed the stack or needs attention:

- Every pull request that still has an uncleared stage, with the stage's outcome and reason.
- Every commit the run pushed, as a Markdown link, and every push that was propagated to descendants.
- A `stopped` result, with its reason and detail preserved exactly: a launch that could not be verified, a stack whose topology changed, a missing stage plugin, or another run holding the lock.
- Any ignored worker result, which means the pull request moved under a worker that was already running.

For `error`, state the error exactly. Never hide a stopped launch, an escalation, or a required action to make the response shorter.

The terminal response is the run's last message.
