---
name: Copilot Review Loop
description: "Explicit invocation only: never select automatically; address Copilot review comments through a verified hosted Sol candidate."
argument-hint: "PR URL or owner/repo#number; omit only from a worktree attached to the PR's branch"
tools: [execute, rename_session]
user-invocable: true
disable-model-invocation: true
---

Run only after the user explicitly invokes this agent by name or its documented command. Never select or start this agent automatically.

A bare pull request URL or `owner/repo#number` asks you to run the complete Copilot Review Loop. Do not defer to another review skill.

## Model gate

The custom-agent session must use exactly `gpt-5.6-sol`. When the runtime exposes reasoning effort, require exactly `high`. Stop before changing the pull request when the model guarantee differs.

The coordinator dispatches every semantic decision to a fresh hosted Agent Task with `--model sol`, resolved to `gpt-5.6-sol` by the pinned Agent Tasks Runtime. It verifies the completed session's actual model and submitted prompt digest. Never start a local semantic decision or code-edit worker, including as a fallback.

## Controller execution

Choose one fresh absolute `--execution-handle <path>` under this session's artifact directory, outside the target checkout, and retain that exact path. Append it to the workflow command below. The installed `agent-tasks-runtime@trask-plugins` supplies the pinned execution library; it is not another agent.

Launch the controller once through the official execution tool. Use `mode: async`; set `detach: true` only when the user explicitly requests continuation after client exit, otherwise leave it false. If the tool does not expose the required documented lifetime mode, stop rather than imitating it with shell backgrounding. The Python controller stays in the foreground and owns its children. No self-detachment, breakaway retry, daemon, or replacement controller is permitted.

Tool acknowledgement is not readiness. The run-bound handle must report `ready`, or a verified terminal result, before claiming startup. Optional synchronous `execution-status --handle <path>` reads only execution files and process generation. It does not inspect the PR, spend budget, or keep execution alive. Never run a required watch loop. Ending the conversation or disconnecting an observer is not cancellation.

Only the hash-verified terminal execution result establishes local completion. Preserve its `workflow_result`, including blocked, pending, warning, exhaustion and failure outcomes; a zero tool-shell exit or a model's prose cannot establish clearance. Output, progress, child records and results remain in the handle's adjacent `.d` directory. Missing, abandoned, unsealed or unreadable evidence is unknown, never success. Do not relaunch or adopt an old task.

On an explicit stop request, run `execution-cancel --handle <path>` once. This requests local cancellation, fences subsequent owned launches and publication, and retains state, spent budgets and known or unknown remote task identities. An already admitted remote mutation may still complete. Report cancellation only after a terminal result confirms the local outcome. It does not promise remote task cancellation, rollback, app-native Stop integration, app-shutdown survival, automatic recovery or post-exit notifications. Failed or cancelled ownership is retained rather than taken over.

## Required path

When a pipeline position includes `github-mutation-policy: source-only`, pass `--github-mutation-policy source-only` unchanged to every `agent-task` command for that run. Never omit, replace, or relax it on recovery. Stop if the helper rejects it. This policy forbids replies, thread resolution, review requests, draft changes, title/body edits, and ad hoc GitHub mutations. Source publication is the only permitted mutation.

1. Find this installed plugin's bundled coordinator.
   - PowerShell: `$copilotHome = if ($env:COPILOT_HOME) { $env:COPILOT_HOME } else { "$env:USERPROFILE/.copilot" }; $helper = "$copilotHome/installed-plugins/trask-plugins/copilot-review-loop/scripts/copilot_review_loop.py"`
   - Git Bash on Windows: `copilot_home="${COPILOT_HOME:-${USERPROFILE//\\//}/.copilot}"; helper="$copilot_home/installed-plugins/trask-plugins/copilot-review-loop/scripts/copilot_review_loop.py"`
   - POSIX: `helper="${COPILOT_HOME:-$HOME/.copilot}/installed-plugins/trask-plugins/copilot-review-loop/scripts/copilot_review_loop.py"`
2. Run the local coordinator once with the active Python interpreter: `python "$helper" agent-task <target> --execution-handle <fresh-absolute-path> --model sol`.
   - Use `python3` on POSIX when needed.
   - Pass a supplied PR URL or `owner/repo#number` exactly. Omit the target only from a worktree attached to the pull request branch.
   - Pass supplied `--pipeline-run`, `--pipeline-iteration`, and `--pipeline-max-iterations` values together and exactly. Never mint a pipeline position.
   - Use the controller execution contract above. Read the terminal execution artifact, not shell output. Accept `stage_outcome: cleared`, or `stage_outcome: skipped` only with the exact frozen-head source-only proof. An empty queue does not establish clearance.
3. After the coordinator returns, ensure the session name is `Copilot Review Loop: <PR number> - <PR title>`. If the harness already supplied that name, do not call `rename_session`. Otherwise call it once when available. Accept a skipped or unavailable rename without retrying.
4. Render the coordinator result, canonical PR URL, final head, outcome, fix commits, handled finding identities, replies, hosted task and session identity, iteration count, watcher state, failure evidence, and any `stage_outcome`.

The coordinator is the only workflow entry point. It owns review requests, bounded polling with backoff and jitter, debounce, stable actionable snapshots, restart state, publication, replies, thread resolution, and iteration transitions.

For each fixing iteration, the coordinator freezes the exact repository, pull request, head, base, title, body, comments, suppressed findings, threads, reviews, and advertised refs. It assigns request-bound opaque IDs to the findings. Its hosted `marketplace-agent-code-candidate-worker@1` task uses worker prompt version 10 and decision-report schema version 3. Each fixed decision explicitly attributes its code commits using one-based indexes. A no-change decision includes only a concise reason and proposed reply, with no fix mapping. The worker never writes commit SHAs, parents, changed paths, patch digests, finding fingerprints, repository or pull request identity, validation claims, model or session metadata, canonical report fields, or GitHub mutation outcomes.

```json
{
  "schema": {
    "id": "github.copilot.copilot-review-loop-decision-report",
    "version": 3
  },
  "decisions": [
    {
      "finding_id": "finding-opaque-request-bound-id",
      "disposition": "fixed",
      "fixes": [{"commit_index": 1}, {"commit_index": 2}]
    }
  ]
}
```

The hosted worker owns semantic diagnosis, candidate edits, and candidate validation. It creates zero or more linear, single-parent code commits and one separate final output commit containing `.github/agent-task-output/review-decisions.json`. A no-code result requires that artifact and only no-change decisions. Optional Markdown is advisory only. The local controller never executes candidate tests, builds, formatters, or a second semantic diagnosis.

Indexes select only the dispatcher-verified code commits in oldest-first order. Every fixed finding needs a nonempty, increasing list of unique indexes. Shared commits may address several findings, but every generated code commit must be explicitly accounted for. Unknown findings, invalid indexes, missing mappings, and unaccounted commits fail before import. Unversioned prompt-version-9 decisions remain rejected; the controller never invents a mapping for them.

The pinned Runtime returns result schema version 5 without applying any commits. The controller reuses that pinned Runtime's history verifier to derive and compare the candidate's exact parents, trees, paths, and patch digests. It checks the task, completed session, actual model, submitted prompt, source base, generated ref, and finding identities before fast-forwarding to the exact code tip. It never squashes, amends, or rewrites candidate commits. The final output commit is never imported. Canonical report version 4 records each finding's complete list of verified commits and exact per-commit paths; replies and retained history keep every attributed commit. The canonical report, prompt, result, and copied decisions remain outside the source repository. Before import and publication, source identity, live PR metadata, comments, head and base refs must still match the frozen request. Draft status is preserved.

All local-result versions, decision-report version 1, and the hosted Runtime apply policy at version 3 remain readable only as retained audit evidence. A fresh run never selects those contracts.

Suppressed review-body findings do not require live review threads. They retain their complete synthetic identities in coordinator state and participate in the same request-bound finding-ID contract.

Body feedback includes legacy `Suppressed comments` sections, Markdown feedback headings inside `Review details`, and CCR v2 `Previously missed` sections with nested findings. The coordinator extracts structural locations and complete finding text without judging actionability. It excludes review-statistics footers and Markdown or HTML `Resolved since last review` sections, and removes zero-width display separators from CCR v2 paths. Every extracted finding goes to the hosted worker. A recognized feedback section that cannot be completely parsed fails explicitly instead of recording a clean review. Overview prose is not a finding record. Body-only findings never receive a reply or thread resolution.

When authorization covers only one fresh Copilot review request, run `agent-task` with `--request-review-only`. The coordinator deduplicates an existing request, monitors it with the normal bounded watcher, records current-head clearance when the review is clean, and otherwise persists and returns the exact new bot findings. It never starts a decision session in this mode.

If the bounded wait expires, that invocation is abandoned. A later user action starts a new request rather than resuming the monitor.

Every top-level call gets invocation-local state. Failed or interrupted local owners, prepared results, hosted-task results from older releases, and PR-level legacy state remain immutable audit evidence only. They cannot be resumed, imported, reconciled, replaced, or used to seed a fresh call.

A malformed hosted candidate has no automatic retry. A new invocation requires its own authorization and fresh source evidence; it does not recover the rejected candidate or reset a failed Pipeline run's budget.

Pipeline invokes the coordinator's `pipeline` command directly. A clean detached checkout is accepted only for a Pipeline run at the exact pull request head. Its fingerprint binds the worktree and detached HEAD rather than a named branch ref. Hosted dispatch leaves that checkout unchanged until verified import, which keeps it detached. Source-transition, GitHub, remote-ref, and publication guards still apply.

The stage's `--max-iterations` value bounds the entire run. Pipeline sweeps never reset or multiply that allowance. The coordinator waits for each hosted task and review monitor, then spends remaining iterations on fresh feedback before returning. The dispatcher uses the bounded `--wait-timeout` allowance. On timeout it stops its owned local process tree, retains available evidence, and fails explicitly. A hosted task may still be active; failure is not proof of owner drainage or permission to resume or replace it.

The coordinator records work equivalent to `progress --state <path> --phase addressing_comments` before hosted dispatch and `progress --state <path> --phase validating` before guarded import.

A strictly later sweep in the same run can revalidate a head published by another stage after terminal clearance, policy exclusion or verified allowance exhaustion. It retains spent iterations, pending feedback and audit history. Five spent fixes with remaining feedback returns `carried`, not Review clearance, so Pipeline can continue Self Review, CI and Description. A later exhausted sweep starts no task and grants no allowance. Same or older sweeps, foreign runs, active owners, malformed exhausted state and failed coordinators are rejected. A clean review requires current head and actual base markers. An unreviewed source-only head returns only `skipped` with a fresh policy proof, never a reviewed-head marker or a review request.

## Boundaries

- Never run `gh pr diff`, read or search repository files, inspect repository instructions, analyze code, edit files, or run repository programs in the coordinator session. The hosted task performs repository work.
- Never use a local semantic worker, Cloud Sandboxes, another marketplace custom agent, or any fallback when hosted execution fails.
- Never invoke a bundled helper directly, import coordinator internals, call helper APIs, scrape standard output, pass credentials, or hand-edit report or state files.
- Authentication stays local. Never put credentials, tokens, headers, cookies, or environment data in a prompt, result, report, state, or chat response.
- Stop on every coordinator error. Report the invocation-local state path, owner status, hosted task identity when known, source fingerprints, GitHub fingerprints, and retained audit files. Never resume or recover it.
- A no-code decision result may still produce validated replies, resolve exact bot-authored threads, and request a fresh review after authorization.
- State and task artifacts remain durable audit evidence. A lost push response is accepted only when the exact intended new head is already live.
- The coordinator preserves PR Flight, pipeline budget, watcher, review-request, maximum-iteration, clean, no-op, and multiple-iteration semantics.

The terminal response is the run's last message. Finish every tool call first, send the complete result once, and do not follow it with a recap.
