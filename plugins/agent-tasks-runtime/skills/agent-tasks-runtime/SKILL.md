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

Policy `marketplace-agent-worker@4` keeps executable validation on the hosted
Agent Task. The worker's final commit contains the report and a minimal JSON
validation array; command strings in that array are inert evidence and are
never run locally. The dispatcher independently verifies the task, request,
policy, source pull request, live head, linear history, changed paths, report,
and validation schema before it applies any fix commit. Fix commits carry only
one unique `Finding:` correlation. The report is nonempty UTF-8 Markdown for
humans and is not parsed as dispatcher attestation. The dispatcher derives the
ordered fix commits from validated Git history and writes that provenance,
trusted identity, artifact digests, and completion state only to the result file
outside the target repository.

If an apply-with-report dispatcher is interrupted after task creation and no
dispatch result survives, `--resume-apply-with-report` can recover the known
task. Recovery reads the hosted task session prompt and requires its exact task,
model, repository, source pull request, policy, report path, and validation path
before normal apply-with-report checks can run. A caller-supplied mode or prompt
cannot establish that provenance. Tasks created under older policy versions
cannot be recovered through a newer contract.
