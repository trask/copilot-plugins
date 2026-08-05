---
name: github-pr-diff-review
description: "Use when the user asks to review a GitHub PR, review a PR URL, review PR <number>, review APR, or perform a local code review of a pull request. Always review only the actual GitHub PR diff from gh pr diff, report findings locally to the user, and never treat the local branch diff as authoritative."
argument-hint: "PR URL, PR number, or owner/repo#number"
---

# GitHub PR Diff Review

Use this skill whenever the user asks to review a GitHub pull request. Phrases like "review PR 123", "review this PR", "review https://github.com/.../pull/123", or "review APR" mean: review the actual GitHub PR diff and report findings locally in chat.

## Rules

- The authoritative changeset is `gh pr diff <pr>`. Do not use `git diff main`, `git diff origin/main...HEAD`, the current branch, or the local working tree as the PR changeset unless the user explicitly asks for a local branch review.
- Treat the PR changeset as the merge-base-to-head diff represented by `gh pr diff`. Never compare the PR head directly with the current base-branch tip and report later base-branch commits as PR-authored deletions or regressions.
- If the PR conflicts with its base branch, report the conflict only as a separate integration risk. Do not infer a conflict resolution or present differences between the current base tip and PR head as review findings.
- Report findings locally in chat only. Do not post GitHub reviews/comments, resolve threads, push commits, edit files, or otherwise act on the PR unless explicitly asked.
- Do not run builds, full test suites, or build-like validation by default. CI handles build verification unless the user explicitly asks to run a specific command.
- Prioritize concrete bugs, regressions, security risks, compatibility issues, broken workflows, and missing tests for changed behavior. Do not report style nits unless they create a real correctness or maintenance risk.
- Follow repo instructions about generated files: review the source that generated them, not generated churn.

Local checkout state may be stale, ahead, behind, or contain unrelated files. Use it only as supporting context after the GitHub PR diff identifies the files and hunks under review.

## Workflow

1. Identify the PR from a URL, number, or `owner/repo#number`. If the repo is unclear, ask one concise question.
2. Fetch PR metadata:

```bash
gh pr view <pr-url-or-number> --json number,title,url,state,baseRefName,baseRefOid,headRefName,headRefOid,headRepositoryOwner,headRepository,author,reviewDecision,mergeable,mergeStateStatus --repo <owner/repo-if-needed>
```

3. Fetch the actual PR file list and patch from GitHub:

```bash
gh pr diff <pr-url-or-number> --repo <owner/repo> --name-only
gh pr diff <pr-url-or-number> --repo <owner/repo>
```

4. Read applicable repository/path instructions, inspect PR hunks, and read file context only for files in the GitHub PR diff or directly needed dependencies. When historical base context matters, use the PR merge base rather than the current base-branch tip.
5. Before reporting a finding, confirm the exact issue is caused by an added, changed, or deleted line in `gh pr diff`. A difference found only by comparing the PR head with the current base-branch tip is not a PR finding.
6. If there are no findings, say so clearly and mention any residual risk from checks intentionally not run.

## Output Format

Findings first, ordered by severity. Each finding should include severity, a clickable file reference, and concise rationale. Then include open questions or assumptions if any, followed by a brief validation summary. If a problem exists only in local checkout state and not in the GitHub PR diff, do not present it as a PR finding.
