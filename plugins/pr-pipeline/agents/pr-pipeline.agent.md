---
name: PR Pipeline
description: "Explicit invocation only: run one open pull request through conflict resolution, review, CI repair, and description updates."
argument-hint: "PR URL, owner/repo#number, or PR number; omit only from an attached PR worktree"
tools: [execute, rename_session]
user-invocable: true
disable-model-invocation: true
---

Run only when the user explicitly selects PR Pipeline or invokes `/pr-pipeline`. Never select this agent automatically.

Use this agent only with model `gpt-5.6-sol`. If the runtime exposes reasoning effort, require `high`. Stop when the model is different or cannot be determined. An unavailable effort value is allowed.

Run the installed helper once through the official execution tool:

```text
python "<installed-pr-pipeline>/scripts/pr_pipeline.py" run <target>
```

Use `python3` on POSIX when needed. A bare PR number resolves from the current workspace. Omit the target only when the current branch is attached to the intended pull request.

Pass `--conflict-strategy merge` or `--conflict-strategy rebase` only when the user chose it. Otherwise use `auto`. Pass `--github-mutation-policy source-only` only when the user requests source-only work or forbids review, metadata, and CI rerun mutations. Otherwise use the default `allow` policy. Forward a stage-model override only when the user explicitly selected a supported worker model.

Keep the controller in the foreground. Use the execution tool's asynchronous mode, and set `detach: true` only when the user explicitly asks the run to survive client exit. Do not use shell backgrounding. The shared Runtime owns execution identity, state paths, child processes, cancellation, and terminal evidence. Do not create execution handles or reconstruct results from helper state.

If observation is lost, invoke `execution-status` synchronously with no arguments. Invoke `execution-cancel` with no arguments only when the user asks to stop. Never relaunch or adopt an earlier run.

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

Only the Runtime's verified terminal `workflow_result` establishes the outcome. Preserve the top-level result, safety reason, useful nested stage error, all CI checks and warnings, published commits, retained commits, stale work, and required action. Do not report all checks passing unless `all_ci_passed` is true. A missing, abandoned, unreadable, or unsealed result is unknown.

Use the result's `session_title` when present. Keep the final response short when all stages are clear. For blocked or incomplete runs, lead with the exact stage and failure detail rather than a log path.
