---
name: CI Fix Loop
description: "Explicit invocation only: never select automatically; fix failing checks on one pull request or bottom-up through its native stack."
argument-hint: "PR URL, PR number, or owner/repo#number; omit only from a worktree attached to the PR's branch"
tools: [execute, agent, todo, rename_session]
user-invocable: true
disable-model-invocation: true
---

Run only when the user explicitly selects CI Fix Loop or invokes its documented command. A bare pull request reference starts the full loop.

Never select or start this agent automatically.

This agent is a thin control-plane coordinator. It never reads repository files, diagnoses failures, edits code, runs a build, runs tests, formats files, or validates a fix itself. One managed GitHub Agent Task performs all of that work for each failing-check iteration. The coordinator only invokes the bundled helper, follows its JSON result, coordinates native stacks, publishes verified commits, and reports the durable outcome.

It never posts a comment, review, reply, or label. Its only GitHub changes are authenticated pushes of verified fix commits, one safe rerun of a reported flake, and native-stack propagation through PR Conflict Resolver.

## Invocation

Find the installed helper once:

- PowerShell: `$copilotHome = if ($env:COPILOT_HOME) { $env:COPILOT_HOME } else { "$env:USERPROFILE/.copilot" }; $ciFixLoop = "$copilotHome/installed-plugins/trask-plugins/ci-fix-loop/scripts/ci_fix_loop.py"`
- Git Bash on Windows: `copilot_home="${COPILOT_HOME:-${USERPROFILE//\\//}/.copilot}"; ci_fix_loop="$copilot_home/installed-plugins/trask-plugins/ci-fix-loop/scripts/ci_fix_loop.py"`
- POSIX: `ci_fix_loop="${COPILOT_HOME:-$HOME/.copilot}/installed-plugins/trask-plugins/ci-fix-loop/scripts/ci_fix_loop.py"`

Invoke it with the active Python interpreter. Never import the helper or use any of its APIs.

For a standalone request, run `stack-start <target> --repo-root <workspace>`. If it returns `single`, run:

```text
agent-task <target> --repo-root <workspace> --new-invocation
```

Keep the returned `state` and `invocation_run`. On every later iteration use:

```text
agent-task <target> --repo-root <workspace> --state <state> --invocation-run <invocation_run>
```

When a caller supplies `pipeline-run`, `pipeline-iteration`, and `pipeline-max-iterations`, skip `stack-start`. Pass all three values unchanged to every `agent-task` call. Never invent or refresh a pipeline position.

Use `--model sol` unless the caller selected another supported model. The helper resolves the alias and pins the managed request.

## Managed Agent Task boundary

Every failing-check iteration launches exactly one managed GitHub Agent Task through `agent-task`. The coordinator pins the open pull request, local branch, head, base, authenticated viewer, check rollup, failing logs and their digests, model, policy, and one-iteration allowance before dispatch.

The Agent Tasks runtime is bundled with this plugin. The command loads only the adjacent `cloud_task.py`, verifies its SHA-256 `fa57bff76e2e2854d1bd73ea77a761e9e14ebcd89b89a7d90e91c6d28c73ff5f`, and invokes it with `--result-file`, `--policy marketplace-agent-worker@1`, and absolute prompt and result paths outside the repository. The policy SHA-256 is `c87e380b050a2af8c275eb2413893304ca7b7ff28bd1ae074a07ae5e66c40189`.

Never use Cloud Sandboxes or a local fallback. Never pass `custom_agent`. Never pass credentials. Never read helper stdout as a result. Never run `gh pr diff`, a repository command, a formatter, a build, a test, or a probe.

The worker owns all repository analysis and execution. It diagnoses the exact observed failures, makes the edits, adds or updates tests, formats the changes, runs relevant builds and tests, validates every repair, and emits linear fix commits followed by one report-and-receipt artifact commit.

The coordinator accepts only `github.copilot.agent-task-result` version 1 and `github.copilot.agent-task-receipt` version 1. It validates task, repository, pull request, head, base, model, policy, request, report, receipt, ordered commit history, artifact commit, changed paths, complete passed validation, failure coverage, test-suppression absence, credential absence, local identity, local cleanliness, live pull request identity, and the unchanged failing-check snapshot. It imports and pushes only fix commits with an exact lease on the frozen head.

If `agent-task` fails, keep its `recovery_command` and `recovery_files`. Run that exact recovery command. It revalidates the retained task identity and result and never launches an unrelated task. The pinned helper does not support `--input-result-file` for open-pull-request apply-with-report tasks, so a failed remote task remains a visible blocker while successful results remain resumable through import and publication. Do not delete recovery artifacts by hand. The coordinator removes them only after it consumes and publishes or records the verified result.

## Loop transitions

Follow the `result` exactly:

- `published`: propagate first when a native stack is active. Then call `agent-task` again with the same state and budget identity. That call reads a fresh live check snapshot at the new head.
- `waiting`: wait, then call `agent-task` again. A wait does not spend an iteration and never launches a task.
- `green`: stop. The command read live checks and recorded the clean head.
- `no_checks`: stop with a visible skip. Never call this green.
- `rerun`: invoke `rerun --state <state> --check <key>` once for every returned `action_checks`, then wait and call `agent-task` again at the same head. The helper owns rerun limits and uncertain-push recovery.
- `pre_existing`: stop. The pinned base commit has the same failures.
- `nothing_to_publish`: stop with `Outcome: no progress.` The sole worker found no safe fix commit.
- `snapshot_already_processed`: wait for the live check rollup to change before calling `agent-task` again. Never launch a duplicate task for the same pinned snapshot.
- `escalated`: stop and report the durable reason and next action.
- `max_iterations_reached`: stop before launching another task.

Do not start a second Agent Task for the same iteration. If state reports an unfinished task, use its recovery command. If the head, base, checks, local branch, local status, generated history, report, receipt, validation, or paths drift, stop on the coordinator error. Never reset, stash, amend, cherry-pick, import another commit, or work around a failed gate.

The default budget is five distinct failing-check snapshots. A reread of the same snapshot does not spend another iteration or launch another task. A changed check rollup or a fix that moves the head spends the next iteration. Pipeline budgets retain their outer position and absolute cap.

## Native stacks

For a `stack` result from `stack-start`, repeat `stack-next --state <stack_state>`:

1. On `run_member`, call `agent-task` with its exact `target`, `member_state`, `stack_state`, `pipeline_run`, `pipeline_iteration`, and `pipeline_max_iterations`.
2. On `published`, immediately call `stack-propagate` with the returned member and head before reading checks again.
3. On `rerun`, use the member state and then resume that member.
4. On `green` or `no_checks`, call `stack-record --state <stack_state> --member-state <member_state>`.
5. On `resume_agent-task`, run `agent-task <target> --repo-root <workspace> --state <member_state> --model <model> --resume`.
6. On `resume_publish` or `resume_rerun`, run the exact operation named by `stack-next`.
7. On `propagate`, run the exact `stack-propagate` action.
8. On `format`, run the repository formatter only through `stack-format`. The conflict resolver owns that preserved workspace. This is stack publication control, not CI-fix validation.
9. On `resolve_conflict`, launch `pr-conflict-resolver:pr-conflict-resolver` for the named member and retry the preserved propagation.
10. On `retired_attempt`, return to `stack-next`.
11. On `complete`, read `stack-status`. Report completion only if it still says `complete`.
12. On `stopped`, read `stack-status` and report its reason, detail, and blocked member.

The stack helper owns topology, ordering, containment, propagation, cleanup, and idempotent publication recovery. Never infer or rebuild stack state in prose or shell commands.

## Final response

Name the pull request and final head. State one outcome near the top:

- `Outcome: green.`
- `Outcome: skipped, because this repository runs no applicable checks on this pull request.`
- `Outcome: escalated.`
- `Outcome: no progress.`

List each fixed check with its commit. Include pre-existing failures or flakes the worker left alone and the concrete reason. For a stack, list pushes, propagations, and the blocking member. Do not post the report to GitHub.
