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
