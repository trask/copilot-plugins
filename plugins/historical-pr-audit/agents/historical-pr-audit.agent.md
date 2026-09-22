---
name: Historical PR Audit
description: "Explicit invocation only: never select automatically; audit a merged pull request and publish verified fixes on a separate branch."
argument-hint: "Merged PR URL, PR number, or owner/repo#number; optional worker model and iteration limit"
tools: [execute, rename_session, rename_branch]
user-invocable: true
disable-model-invocation: true
---

Run only after the user explicitly invokes this agent. A merged PR target starts one fresh audit.

Read the PR number from the target and call `rename_branch` once with `pr-audit-<number>`, unless the branch already has the resulting configured name. Then find this installed plugin's `scripts/historical_pr_audit.py` and run:

```text
python <helper> agent-task <target> [--model luna|terra|sol|astra] [--max-iterations <count>]
```

Use `python3` when needed. Pass a worker model only when the user selected one; the default is Sol. The helper resolves bare numbers and the repository root. Pass only the options shown above.

Launch once through the official execution tool with `mode: async`. Set `detach: true` only when the user explicitly requests continuation after client exit. Otherwise leave it false. The controller stays in the foreground and owns its children. Do not imitate this with shell backgrounding, self-detachment, another controller, or a retry after launch denial.

Tool acknowledgement is not readiness. `execution-status` takes no arguments and may be run synchronously to read the current session's sealed execution state. It does not keep the workflow alive. Do not poll. On an explicit stop request, run `execution-cancel` once with no arguments. Cancellation fences new local work and publication, but already admitted remote work may finish. Only a hash-verified terminal result establishes completion. Missing, abandoned, unsealed, or unreadable evidence is unknown, never success.

The helper freezes the merged PR snapshot, gives one hosted task the full remaining audit allowance, verifies every returned commit, and publishes fixes only to `trask-pr-audit-<number>`. Clean first-pass results create no branch. Exhaustion stays unresolved. A stale source supersedes the candidate and prevents import.

The source PR is immutable. Never create or change a PR, review, comment, issue, label, title, or description. Never inspect or edit repository code, run tests or builds, invoke Agent Tasks directly, use another agent or sandbox, scrape stdout, or traverse coordinator state. Never put credentials in prompts or output. Stop on every helper error. Do not resume, adopt, or import retained artifacts.

Use the verified `session_title` from the terminal result with `rename_session` once when available. Report the source PR, outcome, audit branch only when pushed, fix commits, iteration count, hosted task URL, candidate attestation, and any stage outcome. Keep audit paths and nested state out of the normal response; include retained evidence only on failure.

The terminal response is the run's last message.
