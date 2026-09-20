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
4. Render the coordinator's result, canonical PR URL, final head, outcome, code commits, Agent Task URL, candidate attestation, mechanical coordinator report, iteration count, and any `stage_outcome` field.

When a pipeline position includes `github-mutation-policy: source-only`, pass `--github-mutation-policy source-only` unchanged to every `agent-task` command for that run. Never omit, replace, or relax it. Stop if the helper rejects it. This policy forbids title/body updates, draft changes, comments, thread operations, review requests, and other GitHub metadata mutations. Source publication is the only permitted mutation.

The bundled coordinator is the sole authoritative local entry point. It discovers the separately installed `agent-tasks-runtime@trask-plugins` skill and verifies Runtime 1.0.20 by pinned SHA-256 before execution. The coordinator captures immutable repository, pull request, viewer, publication, and budget identity and dispatches `marketplace-agent-code-candidate-worker@1`. Runtime returns `github.copilot.agent-task-result` version 5 and candidate manifest version 1. The coordinator accepts a terminal completed task with a valid dispatcher-owned candidate and no platform error. It re-derives every commit parent, tree, patch digest, and changed path from fetched Git history before guarded import. The optional final output commit stays outside the code tip.

The worker may make zero or more code commits and must add one final output-only commit containing `.github/agent-task-output/self-review-result.json`. Its only fields are `outcome`, one of `clean`, `exhausted`, or `incomplete`, and integer `iterations_used`. Optional `report.md` is free-form advice; missing or malformed prose cannot reject valid code. The outcome is a hosted semantic claim, not something Git history proves. Zero code commits alone does not establish clean. Existing policy and report parsers remain available only for retained audit evidence.

## Pipeline entrypoint

Pipeline calls the coordinator directly, without another model-driven agent:

```text
python "<helper>" pipeline <target> --state <path> --pipeline-run <run> --pipeline-iteration <sweep> --pipeline-max-iterations <sweeps> --github-mutation-policy source-only --model sol
```

Pass the explicit target and reuse the same state path for this stage throughout one Pipeline run. The checkout may be detached, but it must be clean and at the exact live PR head. A named branch must still match the PR head branch.

The command gives one hosted task the entire remaining allowance. The worker reviews, fixes, validates and corrects internally before returning. The controller verifies provenance and the minimal outcome before import. An explicit clean outcome can clear a code-bearing candidate at the published head and actual base. Exhaustion may publish returned code but remains unresolved. Incomplete or malformed output cannot authorize import.

`--max-iterations` defaults to 5 and bounds hosted review passes across the entire Pipeline run. Count the final no-finding pass too. Clean and exhausted outcomes consume 1 through the assigned allowance; exhaustion must consume it all. One task and one publication can contain several passes and commits. The controller reserves allowance before dispatch; failed or incomplete work cannot replenish it. Later sweeps neither reset nor multiply the budget. Exhaustion returns `stage_outcome: max_iterations_reached`, never a clean marker. Any execution or validation error exits nonzero; an unfinished state cannot be resumed or adopted.

A later sweep may inspect a head published by another stage using the original run's remaining allowance. It requires a completed prior task, a strictly later sweep, and unchanged run, checkout, PR source identity, model, mutation policy, and stage budget. Replaying the same or an older sweep is an error. The synchronous iterations inside one call are not new sweeps.

Source-only skips shared GitHub state publication as well as PR metadata mutation. Audit state stays local.

Atomic state and artifact replacement retries Windows permission errors 5 and 32 up to five times, with 0.38 seconds of total delay. Each attempt uses the same prepared temporary file, without repeating task execution, import, publication, or budget charges. Other errors and exhausted retries remain failures; temporary-file cleanup still runs.

## Boundaries

- Never run `gh pr diff`, read or search repository files, inspect repository instructions, analyze code, make edits, run builds, tests, probes, formatters, hooks, or repository programs locally. Agent Tasks performs every substantive repository action.
- Never use Cloud Sandboxes, marketplace `custom_agent`, a local agent, local analysis or execution, or any fallback when the managed helper fails.
- Never invoke `cloud_task.py` yourself, scrape its standard output, import its final report commit, rerun repository validation locally, or publish with direct commands.
- Authentication stays local. Never put credentials, environment data, tokens, headers, or cookies in a prompt, result, report, state, or chat response.
- Stop on every coordinator error. Report the invocation-local state path, task ID status, task URL or ID, generated branch and head, ordered code commits, optional report evidence, and retained audit artifacts. Never resume, recover, replace, archive, or import that invocation. A later user action starts fresh.
- The coordinator rejects merge commits, unexpected paths or history, malformed or stale candidate manifests, attestation or identity failures, pull request metadata drift, credentials in trusted inputs, local drift, and live head, base, title, or body drift. Never work around a rejection.
- A no-code candidate needs an explicit clean outcome to establish no fixes were needed. Do not push or manufacture a commit.
- State and task artifacts remain durable audit evidence. A lost push response is accepted only when the exact intended new head is already live; no task execution is resumed.
- The helper may publish verified code commits to the pull request's existing head repository and branch. It never changes pull request metadata, posts review comments, or submits a review.

The terminal response is the run's last message. Finish every tool call first, send the complete result once, and do not follow it with a recap.
