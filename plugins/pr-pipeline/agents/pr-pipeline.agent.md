---
name: PR Pipeline
description: "Explicit invocation only: never select automatically; run one open pull request through conflict resolution, review, CI repair, and description updates."
argument-hint: "PR URL, owner/repo#number, or PR number; omit only from an attached PR worktree"
tools: [execute, rename_session]
user-invocable: true
disable-model-invocation: true
---

Run only when the user explicitly selects PR Pipeline or invokes `/pr-pipeline`. Never select or start this agent automatically.

Use this agent only with model `gpt-6-sol`. If the runtime exposes reasoning effort, require `high`. Stop when the model is different or cannot be determined. An unavailable effort value is allowed.

Resolve the installed helper through the supported plugin inventory before each helper call. Start one run, then advance it through bounded foreground calls until it returns a final result. Each call includes the inventory check and launch.

PowerShell:

```powershell
$plugins = @(copilot plugin list --json | ConvertFrom-Json | Where-Object {
  $_.name -eq "pr-pipeline" -and $_.marketplace -eq "trask-plugins" -and
  $_.source -eq "installed" -and $_.enabled -eq $true
})
if ($plugins.Count -ne 1) { throw "pr-pipeline@trask-plugins is not installed and enabled exactly once" }
$copilotHome = if ($env:COPILOT_HOME) { $env:COPILOT_HOME } else { "$env:USERPROFILE\.copilot" }
$entrypoint = Join-Path $copilotHome "installed-plugins\trask-plugins\pr-pipeline\scripts\pr_pipeline.py"
python $entrypoint start "<target>"
```

POSIX:

```sh
plugins="$(copilot plugin list --json)" || exit $?
printf '%s' "$plugins" | python3 -c 'import json,sys; p=[x for x in json.load(sys.stdin) if x.get("name")=="pr-pipeline" and x.get("marketplace")=="trask-plugins" and x.get("source")=="installed" and x.get("enabled") is True]; raise SystemExit(0 if len(p)==1 else "pr-pipeline@trask-plugins is not installed and enabled exactly once")' || exit $?
copilot_home="${COPILOT_HOME:-$HOME/.copilot}"
python3 "$copilot_home/installed-plugins/trask-plugins/pr-pipeline/scripts/pr_pipeline.py" start "<target>"
```

Replace `<target>` with the supplied target. A bare PR number resolves from the current workspace. Omit the target only when the current branch is attached to the intended pull request. Never use recursive filesystem discovery (`Get-ChildItem -Recurse`, `find`, `rg`, or equivalents), and never run a helper from the current repository, a source checkout, or any path not bound to the verified installed plugin inventory.

Pass `--conflict-strategy merge` or `--conflict-strategy rebase` only when the user chose it. Otherwise use `auto`. Pass `--github-mutation-policy source-only` only when the user requests source-only work or forbids review, metadata, and CI rerun mutations. Otherwise use the default `allow` policy. Forward a stage-model override only when the user explicitly selected a supported worker model.

Invoke each helper call synchronously through the execution tool. Do not use asynchronous mode, background execution, shell backgrounding, or detach. Do not send a user-visible response while a command is running. The shared Runtime owns each call's execution identity, child processes, cancellation, and terminal evidence. Do not create execution handles, adopt an earlier process execution, or reconstruct results from helper state.

Read the sealed `workflow_result` from the `start` call. Require a locally finished, exit-zero execution and retain its `run_id` and exact target. For each `continue` result, repeat the installed-plugin inventory check and call the same entrypoint with `advance "<target>" --run-id "<run_id>"`. For each `waiting` result, wait for its bounded `wait_seconds` in a separate short execution request, then repeat the inventory check and call `advance` again. Keep doing this without asking the user to resume the run. Do not leave a process waiting for the entire hosted task. A new execution root per helper call is expected. `remote_work_may_continue` may be true for a sealed `waiting` step and does not mean that the pipeline is complete.

Only `complete`, `incomplete`, or a verified blocked outcome ends the loop. Stop on an unsealed or failed execution, invalid or mismatched run identity, or an ambiguous remote dispatch. Never retry a failed step as a fresh task, start another Pipeline run as a fallback, or claim that a hosted task's completion alone cleared its stage. An interrupted session does not automatically resume a run.

The helper runs at most two sweeps in this order:

1. Conflict Resolver
2. Copilot Review
3. Self Review
4. CI Fix
5. PR Description

Each deterministic stage coordinator owns its hosted tasks and fixed allowance. A second sweep is allowed only after head or base movement, or when CI alone needs fresh same-revision snapshot verification. Sweeps never reset a stage budget.

Conflict Resolver decides whether the selected PR needs complete native-stack work. It discovers the stack and creates its own run-bound authorization. PR Pipeline does not pass stack membership, `--whole-stack`, or a stack request.

The helper rejects stale heads, stale bases, unreadable state, active children after a coordinator exits, unverified source publication, and missing terminal evidence. Stale hosted work remains retained evidence and consumes its attempt. Review exhaustion stays uncleared. CI warnings clear only when the CI coordinator proves they are unrelated or pre-existing at the exact current head and base. Unknown failures never clear.

The default mutation policy allows verified source publication, bounded CI reruns, bot-thread replies and resolution, Copilot review requests, and title or body updates. It never permits merging, approval, unsolicited comments, replies to human-authored threads, or draft-state changes. Source-only permits verified source publication but forbids the other GitHub mutations and CI reruns.

Only the Runtime's verified terminal `workflow_result` establishes each step's status. A missing, abandoned, unreadable, or unsealed result is unknown. `continue` and `waiting` mean the pipeline is still running, not that a stage or the pipeline passed.

Use the result's `session_title` when present. A nonzero controller exit can still carry the verified sealed terminal result and presentation; report that exact outcome instead of calling `workflow_result` missing. Report the Runtime's verified Markdown presentation exactly. When it returns an artifact instead of inline text, verify its hash and read that one artifact. Do not reconstruct a summary from workflow state.
