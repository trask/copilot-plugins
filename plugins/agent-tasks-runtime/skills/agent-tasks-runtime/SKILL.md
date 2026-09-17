---
name: agent-tasks-runtime
description: Internal GitHub Agent Tasks runtime dependency for Trask pull request agents. Use only when maintaining or diagnosing those agents' shared cloud execution backend.
---

# Agent Tasks Runtime

This skill packages the shared Agent Tasks helper used by Trask pull request
agents. It is an internal runtime dependency, not a user-facing workflow.

Consumer coordinators discover this skill through `copilot skill list --json`,
pin the exact helper digest, and invoke the helper directly. Do not invoke its
scripts manually during an agent workflow.

Policy `marketplace-agent-worker@5` keeps executable validation on the hosted
Agent Task. The worker's final commit contains the report and a minimal JSON
validation array of exact `command` and `outcome` objects; `outcome` must be
`passed`. Command strings in that array are inert evidence and are
never run locally. The dispatcher independently verifies the task, request,
policy, source pull request, live head, linear history, changed paths, report,
and validation schema before it applies any fix commit. Fix commits carry only
one unique `Finding:` correlation. The report is nonempty UTF-8 Markdown for
humans and is not parsed as dispatcher attestation. The dispatcher derives the
ordered fix commits from validated Git history and writes that provenance,
trusted identity, artifact digests, and completion state only to the result file
outside the target repository.

Consumer prompts may use `{{MARKETPLACE_REPORT_PATH}}` and
`{{MARKETPLACE_VALIDATION_PATH}}`. The dispatcher replaces them with the exact
request-scoped paths before task creation so workflow instructions and the final
policy block name the same artifacts. Alternate or scratch artifact paths must
never be committed.

Policy `marketplace-agent-report-worker@1` is a separate report-only contract.
It permits only `--report` tasks and requires exactly one generated commit,
directly on the immutable source head, that changes only the assigned nonempty
UTF-8 Markdown report. It has no worker validation artifact or executable
validation claim. Result schema version 2 records
`attestation.kind=dispatcher_structural` and reports only the identity, history,
path, and digest facts independently established by the dispatcher. Report
content remains untrusted inert evidence for a consumer to evaluate.

Policy `marketplace-agent-apply-report-worker@4` is the current apply contract.
The worker writes one request-scoped
`github.copilot.agent-task-semantic-output` version 1 JSON artifact and may
produce ordered fix commits. The semantic payload contains only
workflow-specific findings, decisions, and validation evidence. It cannot
contain dispatcher-owned request, repository, pull request, frozen head or
base, model, policy, task, session, generated-ref, commit, report, receipt, or
validation-completion identity. Findings refer to generated commits only by
one-based `commit_index`.

The dispatcher derives the complete fix history from Git, resolves every commit
index, requires the payload to account for every generated fix commit, and
returns result schema version 3 with
`attestation.kind=dispatcher_semantic`. Consumers combine that payload with
their frozen local request to generate the canonical workflow report and then
run their workflow-specific consistency checks before guarded publication.
Missing or malformed semantic output, unaccounted commits, stale frozen
identity, and clean outcomes with nonempty fix history fail closed.

Validation evidence remains semantic worker output because the dispatcher
cannot truthfully claim commands it did not execute. It is never promoted to a
dispatcher validation claim or inferred from prose or standard output.

Policy `marketplace-agent-apply-report-worker@3` remains parseable only as
immutable legacy evidence under that exact contract. It permits zero or more
ordered fix commits followed by exactly one
dispatcher-assigned Markdown report commit. The dispatcher records result schema version 2 with
`attestation.kind=dispatcher_structural` and
`application.status=not_applied`. Legacy report content stays untrusted and
must pass the original consumer validation; it is never normalized into a
version 4 semantic result.

Policy `marketplace-agent-apply-report-worker@2` remains parseable only as
immutable legacy evidence under that exact contract. It applied fix commits
before the consumer validated its workflow-specific report. It cannot seed or
resume a current invocation.

Policy `marketplace-agent-apply-report-worker@1` remains parseable only as
immutable legacy evidence under that exact contract. It also requires each
fix commit message to contain exactly one nonempty `Finding:` line. Version 2
removed that model-authored duplicate because consumers validate the stronger
report mapping.

Each dispatcher call is a fresh invocation. It never accepts a task ID, prior
result, monitor-only mode, or resume mode as execution input. Interrupted and
failed invocations remain immutable audit evidence; a later call creates a new
request and task with a new invocation-local result. The caller must still pin
the current source identity, and all normal structural and semantic checks
remain fail closed.
If the creation request itself fails, the caller's pinned result-file path
keeps request ownership and the result keeps repository, pull request, model,
and policy identity. Every task, generated branch, report, receipt, and
semantic-output identity remains null.
