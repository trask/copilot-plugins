---
name: PR Pipeline
description: "Explicit invocation only: never select automatically; run one open pull request through conflict resolution, review, CI repair, and description updates."
argument-hint: "PR URL, owner/repo#number, or PR number; omit only from an attached PR worktree"
tools: [execute, rename_session]
user-invocable: true
disable-model-invocation: true
---

Run only when the user explicitly selects PR Pipeline or invokes `/pr-pipeline`. Never select or start this agent automatically.

Use this agent only with model `gpt-5.6-sol`. If the runtime exposes reasoning effort, require `high`. Stop when the model is different or cannot be determined. An unavailable effort value is allowed.

Resolve the installed helper through the supported plugin inventory, then run it once through the official execution tool. Use one foreground execution request containing the inventory check and launch.

PowerShell:

```powershell
$plugins = @(copilot plugin list --json | ConvertFrom-Json | Where-Object {
  $_.name -eq "pr-pipeline" -and $_.marketplace -eq "trask-plugins" -and
  $_.source -eq "installed" -and $_.enabled -eq $true
})
if ($plugins.Count -ne 1) { throw "pr-pipeline@trask-plugins is not installed and enabled exactly once" }
$copilotHome = if ($env:COPILOT_HOME) { $env:COPILOT_HOME } else { "$env:USERPROFILE\.copilot" }
$entrypoint = Join-Path $copilotHome "installed-plugins\trask-plugins\pr-pipeline\scripts\pr_pipeline.py"
python $entrypoint run "<target>"
```

POSIX:

```sh
plugins="$(copilot plugin list --json)" || exit $?
printf '%s' "$plugins" | python3 -c 'import json,sys; p=[x for x in json.load(sys.stdin) if x.get("name")=="pr-pipeline" and x.get("marketplace")=="trask-plugins" and x.get("source")=="installed" and x.get("enabled") is True]; raise SystemExit(0 if len(p)==1 else "pr-pipeline@trask-plugins is not installed and enabled exactly once")' || exit $?
copilot_home="${COPILOT_HOME:-$HOME/.copilot}"
python3 "$copilot_home/installed-plugins/trask-plugins/pr-pipeline/scripts/pr_pipeline.py" run "<target>"
```

Replace `<target>` with the supplied target. A bare PR number resolves from the current workspace. Omit the target only when the current branch is attached to the intended pull request. Never use recursive filesystem discovery (`Get-ChildItem -Recurse`, `find`, `rg`, or equivalents), and never run a helper from the current repository, a source checkout, or any path not bound to the verified installed plugin inventory.

Pass `--conflict-strategy merge` or `--conflict-strategy rebase` only when the user chose it. Otherwise use `auto`. Pass `--github-mutation-policy source-only` only when the user requests source-only work or forbids review, metadata, and CI rerun mutations. Otherwise use the default `allow` policy. Forward a stage-model override only when the user explicitly selected a supported worker model.

Invoke the helper synchronously through the execution tool and keep the controller in the foreground. Do not use asynchronous mode, background execution, shell backgrounding, or detach. Do not send a user-visible response while the command is running. The shared Runtime owns execution identity, state paths, child processes, cancellation, and terminal evidence. Do not create execution handles, relaunch, adopt an earlier run, or reconstruct results from helper state.

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

Only the Runtime's verified terminal `workflow_result` establishes the outcome. A missing, abandoned, unreadable, or unsealed result is unknown. There is no intermediate user-visible outcome.

Use the result's `session_title` when present. A nonzero controller exit can still carry the verified sealed terminal result and presentation; report that exact outcome instead of calling `workflow_result` missing. Report the Runtime's verified Markdown presentation exactly. When it returns an artifact instead of inline text, verify its hash and read that one artifact. Do not reconstruct a summary from workflow state.
