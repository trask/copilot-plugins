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

Policy `marketplace-agent-worker@2` keeps executable validation on the hosted
Agent Task. The worker's final commit contains the report and a minimal JSON
validation array; command strings in that array are inert evidence and are
never run locally. The dispatcher independently verifies the task, request,
policy, source pull request, live head, linear history, changed paths, report,
and validation schema before it applies any fix commit. It writes the trusted
identity, artifact digests, and completion state only to the result file
outside the target repository.
