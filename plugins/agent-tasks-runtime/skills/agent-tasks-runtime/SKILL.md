---
name: agent-tasks-runtime
description: Internal GitHub Agent Tasks runtime dependency for Trask pull request agents. Use only when maintaining or diagnosing those agents' shared cloud execution backend.
---

# Agent Tasks Runtime

This is the shared runtime dependency for Trask PR agents, not a user workflow. Consumer coordinators discover it through `copilot skill list --json`, verify the exact source digest, and invoke the helper. Do not invoke its scripts manually during an agent workflow.

`scripts/execution.py` is the shared foreground execution library. All nine
user-facing entrypoints pin its source bytes. Pipeline and Conflict use this
library for local ownership without changing Conflict's dedicated hosted
backend. It supplies fresh generation-bound root and child identities,
file-backed output and canonical terminal results, optional read-only status,
explicit local cancellation and conservative branch-writer leases. Windows
completion requires a zero active-job count; job membership and exact process
handles distinguish a running descendant from terminated accounting residue.

Controllers remain foreground processes. Only the official execution tool may
detach a root when the user explicitly requests survival beyond client exit.
There is no fallback from denied legacy breakaway, self-detach layer, daemon,
automatic recovery or app-native Stop integration. Readiness comes from the
controller, not the tool acknowledgement. Completion comes from a verified
terminal file, not shell exit or model prose.

Cancellation fences later owned subprocess launches and publication. It does
not retract an admitted remote mutation or prove remote task cancellation.
Dispatch observations retain creation uncertainty and known task identities.
Domain state retains its original budgets and pending outcomes. Failed,
cancelled or abandoned ownership cannot be adopted by a new invocation.
Writer releases take effect only with the owner's exact sealed terminal
result. A missing or unsealed result keeps the branch unavailable even when
the controller has exited.
Failed, cancelled, missing or remotely unconfirmed child execution evidence
keeps root ownership retained, including when Pipeline reports `incomplete`
or Stack Pipeline reports `partial`.
Terminal diagnostics retain identical evidence once per source path, in
first-seen order. Conflicting hashes or observations keep their distinct
versions and fail finalization; separate unknown task creations remain separate.
No production lifetime or graceful app-shutdown guarantee follows from the
single inert Windows controlled-client-exit qualification.

## Current contracts

`marketplace-agent-code-candidate-worker@1` permits zero or more linear single-parent code commits and an optional final output-only commit under `.github/agent-task-output/`. Review, Self Review, CI Fix and Historical Audit use it.

`marketplace-agent-report-recommendation-worker@1` forbids code commits and requires exactly one final output-only commit. PR Description and both PR Reviewer phases use it.

Both return `github.copilot.agent-task-result` version 5 and `github.copilot.agent-task-candidate-manifest` version 1. The dispatcher binds the fresh completed task and its only session to the requested repository, actual model, complete submitted prompt, source and generated ref. It derives commit SHAs, parents, trees, patch digests, changed paths and the code tip from Git. Completion evidence retains timestamps and the final API response digest. Consumers rederive and compare provenance with their own frozen requests before import or publication.

The final artifact commit is excluded from `candidate.generated.code_tip_sha`. The dispatcher never imports candidate commits. Unsafe paths, mixed code/output commits, nonlinear history, extra sessions, source/model/prompt drift and incomplete execution fail closed.

`.github/agent-task-output/report.md` is optional free-form advice. Missing, empty or malformed prose cannot reject otherwise valid candidate code. Small workflow-specific semantic outputs remain necessary:

- Review dispositions bind opaque finding IDs and commit indexes.
- Self Review and Historical Audit return only clean/exhausted/incomplete and consumed review-pass counts.
- CI returns diagnosis or rerun recommendations when needed.
- Reviewer discovery returns anchored findings and evidence; separate fresh Astra critique returns selected IDs and exact final comment bodies.
- Description returns proposed title/body files.

These outputs contain semantic claims, not model-authored provenance. Task completion, candidate acceptance/publication and workflow clearance are separate facts. Git cannot prove a semantic review was clean, and neither a candidate nor a hosted assertion proves CI green.

All repository analysis, edits, tests and internal corrections stay hosted. Local controllers manage identities, permissions, budgets, import/publication, actual rerun requests and fresh GitHub observations. They do not execute candidate code or a model-authored validation command.

## Source binding and fresh execution

Ordinary code-candidate consumers require an open PR and current source identity. A detached checkout is allowed only when HEAD already equals the frozen PR head; it is never realigned. Named branch, HEAD, clean-worktree and operation guards otherwise remain in force. Live source drift after task completion rejects acceptance while preserving the completed task identity as evidence.

Historical Audit explicitly supplies `--allow-merged-pr` with its trusted immutable merged-PR snapshot. This code-candidate exception requires `trask-pr-audit-<number>` at that exact historical head and dispatches from the immutable SHA. Worker output cannot enable it. It does not relax ordinary open-PR guards or permit report-recommendation consumers to use merged sources.

Each call is fresh. No task ID, prior result, resume, monitor-only mode or retained historical invocation can become execution input. Interrupted work remains immutable evidence. A same-active-call exact-lease publication confirmation is different from later retained-state re-entry.

Older apply/report and semantic formats remain compatibility code. Current consumers explicitly select the two version-5 contracts above; they do not normalize, repair or import old results into a new invocation. Conflict Resolver has its own pinned runtime and versioned request contract.
