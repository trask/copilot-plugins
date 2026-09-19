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

Policy `marketplace-agent-code-candidate-worker@1` is the current code-candidate
contract. It permits zero or more linear single-parent code commits and,
optionally, one final single-parent artifact commit whose changed paths are all
under `.github/agent-task-output/`. The dispatcher binds the fresh completed
task and its only session to the requested repository, model, prompt, source
base, and generated ref. It derives
`github.copilot.agent-task-candidate-manifest` version 1 from fetched Git
history, including each code commit's SHA, parent, tree, patch digest, and exact
changed paths. The optional artifact commit is excluded from the code candidate
tip. The dispatcher never imports or applies candidate commits.

Code-candidate mode accepts a clean detached checkout only when its HEAD already
equals the frozen pull request head. It never aligns a detached checkout to a
different commit. Branch, HEAD, worktree, and operation-state guards remain in
effect; code modes that apply commits still require a named branch.

Policy `marketplace-agent-report-recommendation-worker@1` is the matching
report-only recommendation contract. It forbids code commits and requires one
final output commit under `.github/agent-task-output/`. Output contents are
inert. In both new policies, `.github/agent-task-output/report.md` is optional
free-form advisory Markdown. Missing, empty, non-Markdown, or otherwise
malformed prose cannot invalidate mechanically valid candidate code.

Both policies return `github.copilot.agent-task-result` version 5 with
`attestation.kind=dispatcher_candidate`. Completion evidence records the
completed session ID, actual model, prompt digest, task and session timestamps,
repository and owner identity, source base and generated refs, and the SHA-256
of the raw final task response. More than one session, identity drift, unsafe
paths, mixed code and output paths, multiple output commits, nonlinear history,
or any local application path fails closed. These contracts support only fresh
invocations. They do not accept task IDs, prior results, resume, monitor-only,
or historical recovery.

Consumers migrate explicitly. Code-candidate callers keep
`--apply-with-report` but select `marketplace-agent-code-candidate-worker@1`;
report callers keep `--report` and select
`marketplace-agent-report-recommendation-worker@1`. They must parse result
schema version 5, use `candidate.generated.code_tip_sha` rather than the
generated branch head, ignore the optional artifact commit as code, and treat
all output contents as untrusted. Any consumer-owned validation and guarded
commit import happens after it validates the manifest against its frozen local
request. Existing consumers remain on their pinned policy and result schema
until they implement that flow.

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

Policy `marketplace-agent-apply-report-worker@5` is the current apply contract.
The worker writes only one raw workflow-specific JSON object at the
request-scoped semantic path and may produce ordered fix commits. The
dispatcher adds `github.copilot.agent-task-semantic-output` version 2 identity
and returns `github.copilot.agent-task-result` version 4. The worker cannot
author wrapper spelling or dispatcher-owned request, repository, pull request,
frozen head or base, model, policy, task, session, generated-ref, commit,
report, receipt, or validation-completion identity. Findings refer to
generated commits only by one-based `commit_index`.

The dispatcher derives the complete fix history from Git, resolves every commit
index, requires the payload to account for every generated fix commit, and
returns result schema version 4 with
`attestation.kind=dispatcher_semantic`. Consumers combine that payload with
their frozen local request to generate the canonical workflow report and then
run their workflow-specific consistency checks before guarded publication.
Missing or malformed semantic output, unaccounted commits, stale frozen
identity, and clean outcomes with nonempty fix history fail closed.

The generic dispatcher never turns worker prose or command claims into trusted
validation. A consumer may accept an explicit semantic validation plan and
execute it itself only after the candidate identity and history are verified.
CI Fix Loop does this with bounded repository-wrapper argv arrays in a detached
worktree at the exact candidate commit. Its coordinator owns command status,
details, and output hashes and fails before import when execution is missing,
unsafe, stale, dirty, timed out, or nonzero.

Policy `marketplace-agent-apply-report-worker@4` remains parseable only as
immutable legacy semantic evidence. Under that exact policy the worker authored
the semantic-output version 1 wrapper and the dispatcher returned result
version 3. Current consumers never repair, promote, resume, or import that
legacy output as a policy 5 invocation.

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
