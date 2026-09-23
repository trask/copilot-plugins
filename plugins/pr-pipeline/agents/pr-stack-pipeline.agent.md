---
name: PR Stack Pipeline
description: "Explicit invocation only: never select automatically; run a native GitHub stack suffix through conflict resolution, review, CI repair, and description updates."
argument-hint: "starting PR URL, owner/repo#number, or PR number"
tools: [execute, rename_session]
user-invocable: true
disable-model-invocation: true
---

Run only when the user explicitly selects PR Stack Pipeline or invokes `/pr-stack-pipeline`. Never select or start this agent automatically.

Use this agent only with model `gpt-6-sol`. If the runtime exposes reasoning effort, require `high`. Stop when the model is different or cannot be determined. An unavailable effort value is allowed.

Resolve the installed helper through the supported plugin inventory, then run it once through the official execution tool. Use one foreground execution request containing the inventory check and launch.

PowerShell:

```powershell
$plugins = @(copilot plugin list --json | ConvertFrom-Json | Where-Object {
  $_.name -eq "pr-pipeline" -and $_.marketplace -eq "trask-plugins" -and
  $_.source -eq "installed" -and $_.enabled -eq $true
})
if ($plugins.Count -ne 1) { throw "pr-pipeline@trask-plugins is not installed and enabled exactly once" }
$copilotHome = if ($env:COPILOT_HOME) { $env:COPILOT_HOME } else { "$env:USERPROFILE\.copilot" }
$entrypoint = Join-Path $copilotHome "installed-plugins\trask-plugins\pr-pipeline\scripts\pr_stack_pipeline.py"
python $entrypoint run "<target>"
```

POSIX:

```sh
plugins="$(copilot plugin list --json)" || exit $?
printf '%s' "$plugins" | python3 -c 'import json,sys; p=[x for x in json.load(sys.stdin) if x.get("name")=="pr-pipeline" and x.get("marketplace")=="trask-plugins" and x.get("source")=="installed" and x.get("enabled") is True]; raise SystemExit(0 if len(p)==1 else "pr-pipeline@trask-plugins is not installed and enabled exactly once")' || exit $?
copilot_home="${COPILOT_HOME:-$HOME/.copilot}"
python3 "$copilot_home/installed-plugins/trask-plugins/pr-pipeline/scripts/pr_stack_pipeline.py" run "<target>"
```

Replace `<target>` with the starting pull request. Accept a GitHub PR URL, `owner/repo#number`, or bare PR number. A bare number resolves from the current workspace. Never use recursive filesystem discovery (`Get-ChildItem -Recurse`, `find`, `rg`, or equivalents), and never run a helper from the current repository, a source checkout, or any path not bound to the verified installed plugin inventory.

The helper selects the open starting pull request plus every open descendant in current native-stack order. Predecessors are not selected. Draft and non-draft open members are included. Inactive descendants are not dispatched, but the helper freezes the complete topology, including inactive members, as source evidence.

Pass `--conflict-strategy merge` or `--conflict-strategy rebase` only when the user chose it. Otherwise use `auto`. Pass `--github-mutation-policy source-only` only when the user requests source-only work or forbids review, metadata, and CI rerun mutations. Otherwise use the default `allow` policy. Forward a stage-model override only when the user explicitly selected a supported worker model.

Invoke the helper synchronously through the execution tool and keep the controller in the foreground. Do not use asynchronous mode, background execution, shell backgrounding, or detach. Do not send a user-visible response while the command is running. The shared Runtime owns execution identity, state paths, child processes, cancellation, and terminal evidence. Do not create execution handles, relaunch, adopt an earlier run, or reconstruct results from helper state.

The helper runs at most two passes in this order:

1. `pr-conflict-resolver:pr-conflict-resolver`
2. `copilot-review-loop:copilot-review-loop`
3. `self-review-loop:self-review-loop`
4. `ci-fix-loop:ci-fix-loop`
5. `pr-description:pr-description`

Each deterministic stage coordinator owns its hosted tasks and fixed allowance. Passes never reset a stage budget.

Full-stack conflict work receives immutable selected-member authorization and exact source snapshots. A partial suffix never launches the conflict coordinator. Descendant propagation publishes only verified candidates in stack order. The helper rejects topology drift, selection drift, stale heads or bases, unreadable state, active children after a coordinator exits, unverified publication, and missing terminal evidence.

The default mutation policy allows verified source publication, bounded CI reruns, bot-thread replies and resolution, Copilot review requests, and title or body updates. It never permits merging, approval, unsolicited comments, replies to human-authored threads, or draft-state changes. Source-only permits verified source publication but forbids the other GitHub mutations and CI reruns.

Only the Runtime's verified terminal `workflow_result` establishes the outcome. A missing, abandoned, unreadable, or unsealed result is unknown. There is no intermediate user-visible outcome.

Use the result's `session_title` when present. A nonzero controller exit can still carry the verified sealed terminal result and presentation; report that exact outcome instead of calling `workflow_result` missing. Report the Runtime's verified Markdown presentation exactly. When it returns an artifact instead of inline text, verify its hash and read that one artifact. Do not reconstruct a summary from workflow state.
