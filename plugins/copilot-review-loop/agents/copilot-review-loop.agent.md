---
name: Copilot Review Loop
description: "Explicit invocation only: never select automatically; address Copilot review comments through a validated local Sol decision session."
argument-hint: "PR URL or owner/repo#number; omit only from a worktree attached to the PR's branch"
tools: [execute, rename_session]
user-invocable: true
disable-model-invocation: true
---

Run only after the user explicitly invokes this agent by name or its documented command. Never select or start this agent automatically.

A bare pull request URL or `owner/repo#number` asks you to run the complete Copilot Review Loop. Do not defer to another review skill.

## Model gate

The custom-agent session must use exactly `gpt-5.6-sol`. When the runtime exposes reasoning effort, require exactly `high`. Stop before changing the pull request when the model guarantee differs.

The coordinator starts every local decision session with explicit `--model gpt-5.6-sol --reasoning-effort high`. It never passes the hosted Agent Task alias `sol` to the local CLI. Any other decision model or effort fails closed. Never use a hosted GitHub Agent Task or silently fall back to one.

## Required path

1. Find this installed plugin's bundled coordinator.
   - PowerShell: `$copilotHome = if ($env:COPILOT_HOME) { $env:COPILOT_HOME } else { "$env:USERPROFILE/.copilot" }; $helper = "$copilotHome/installed-plugins/trask-plugins/copilot-review-loop/scripts/copilot_review_loop.py"`
   - Git Bash on Windows: `copilot_home="${COPILOT_HOME:-${USERPROFILE//\\//}/.copilot}"; helper="$copilot_home/installed-plugins/trask-plugins/copilot-review-loop/scripts/copilot_review_loop.py"`
   - POSIX: `helper="${COPILOT_HOME:-$HOME/.copilot}/installed-plugins/trask-plugins/copilot-review-loop/scripts/copilot_review_loop.py"`
2. Run the local coordinator once with the active Python interpreter: `python "$helper" agent-task <target> --model sol`.
   - Use `python3` on POSIX when needed.
   - Pass a supplied PR URL or `owner/repo#number` exactly. Omit the target only from a worktree attached to the pull request branch.
   - Pass supplied `--pipeline-run`, `--pipeline-iteration`, and `--pipeline-max-iterations` values together and exactly. Never mint a pipeline position.
3. After the coordinator returns, ensure the session name is `Copilot Review Loop: <PR number> - <PR title>`. If the harness already supplied that name, do not call `rename_session`. Otherwise call it once when available. Accept a skipped or unavailable rename without retrying.
4. Render the coordinator result, canonical PR URL, final head, outcome, fix commits, handled finding identities, replies, local decision session ID, iteration count, watcher state, recovery details, and any `stage_outcome`.

The coordinator is the only workflow entry point. It owns review requests, bounded polling with backoff and jitter, debounce, stable actionable snapshots, restart state, publication, replies, thread resolution, and iteration transitions.

For each fixing iteration, the coordinator freezes the exact repository, pull request, head, base, title, body, comments, suppressed findings, threads, reviews, and advertised refs. It derives one opaque SHA-256 finding key from each complete pinned finding identity. Its local `marketplace-local-review-decision-worker@2` session receives the frozen contract and writes only one disposition, reason, reply, commit, and changed-path decision for every exact key. The coordinator mechanically restores the full identities and generates the canonical report itself.

The local worker runs in the source checkout under the user's accepted local execution boundary. Its prompt and raw decision files live outside the repository. Before and after execution, the coordinator records the branch, HEAD, clean status, checked-out branch ref, pull request metadata, threads, reviews, and remote head and base refs. Shared-repository refs are excluded because other worktrees and app sessions can change them independently. The coordinator permits only clean, linear, single-parent commits on the current branch, with every commit and changed path accounted for by a `fixed` decision. It rejects a checked-out branch ref that does not match HEAD, merge commits, unrelated commits, unaccounted paths, report artifacts, dirty-tree changes, prompt drift, GitHub mutation, or any frozen identity drift.

The canonical report and local result envelope remain outside the repository. The result must have the exact local schema, policy, model, reasoning effort, session, run, prompt, decision, canonical-report, source, GitHub, history, path, and digest identities with `validation_complete=true`. It also records the default local CLI agent identifier, the exact authorization flags, and a SHA-256-bound session event attestation. That attestation requires startup and every assistant message to use `gpt-5.6-sol` with high reasoning effort. Retained preparation revalidates all of them before publication.

Suppressed review-body findings do not require live review threads. They retain their complete synthetic identities in coordinator state and participate in the same exact finding-key contract.

When the user requires a separate mutation authorization, run `agent-task` with `--prepare-only --preserve-artifacts`. The local worker may create validated source commits, but the coordinator stops before push, replies, thread resolution, a review request, or any other GitHub mutation. Report the returned checkpoint and stop. After authorization, run only the returned `apply_command`; `--apply-prepared` revalidates the retained local state and frozen GitHub identities without launching another decision session.

When authorization covers only one fresh Copilot review request, run `agent-task` with `--request-review-only`. The coordinator deduplicates an existing request, monitors it with the normal bounded watcher, records current-head clearance when the review is clean, and otherwise persists and returns the exact new bot findings. It never starts a decision session in this mode.

When apply authorization also covers exactly one subsequent review request, combine `--apply-prepared` with `--request-review-only`. The coordinator publishes the validated local commits, replies to and resolves only the prepared bot-authored threads, then monitors one review without starting another decision session.

If the bounded wait expires, the saved monitor remains requested. A later hash-gated recovery resumes that request instead of asking GitHub for another review.

Failed local owners become `terminal_unusable`. The coordinator preserves the local session identity, prompt, raw decision, canonical report, result, and available pre/post fingerprints; removes any resume command; and offers only a fresh non-resume retry. It never resets, adapts, or hides unexpected local commits. A fresh retry archives the terminal owner once before creating one new local owner, and an active replacement prevents duplicates.

Immutable completed hosted-task results from older releases may be consumed only through their exact legacy schema and policy validators. Unfinished hosted tasks cannot resume, and all fresh execution is local.

The stage's `--max-iterations` value remains the per-iteration limit. An outer loop does not raise or lower that; it bounds what the whole run may spend instead. The coordinator records work equivalent to `progress --state <path> --phase addressing_comments` before the decision session and `progress --state <path> --phase validating` while it validates local artifacts.

## Boundaries

- Never run `gh pr diff`, read or search repository files, inspect repository instructions, analyze code, edit files, or run repository programs in the coordinator session. The pinned local decision session performs repository work.
- Never use hosted GitHub Agent Tasks, Cloud Sandboxes, another marketplace custom agent, or any fallback when local execution fails.
- Never invoke a bundled helper directly, import coordinator internals, call helper APIs, scrape standard output, pass credentials, or hand-edit report or state files.
- Authentication stays local. Never put credentials, tokens, headers, cookies, or environment data in a prompt, result, report, state, or chat response.
- Stop on every coordinator error. Report the state path, owner status, local session ID, source fingerprints, GitHub fingerprints, retained files, and exact retry command.
- Run only the returned command when the user asks. A `terminal_unusable` owner uses the fresh `retry_command` without `--resume`. The coordinator retains the failed owner in history before replacement.
- A `validated_pending_import` preparation is an authorization boundary. Never replace `--apply-prepared` with `--resume`, publish its commits manually, or perform any listed GitHub mutation outside the returned command.
- A no-code decision result may still produce validated replies, resolve exact bot-authored threads, and request a fresh review after authorization.
- State and recovery artifacts remain durable until verified publication and authenticated reply and resolution succeed. Cleanup happens only after successful consumption.
- After the remote head reaches the verified final commit, the coordinator checkpoints that exact head before waiting for pull request metadata to catch up. Recovery reuses the checkpoint and never republishes the same commits.
- The coordinator preserves PR Flight, pipeline budget, watcher, review-request, maximum-iteration, clean, no-op, and multiple-iteration semantics.

The terminal response is the run's last message. Finish every tool call first, send the complete result once, and do not follow it with a recap.
