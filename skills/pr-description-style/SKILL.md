---
name: pr-description-style
description: "Use when creating or editing GitHub pull request descriptions, draft PR bodies, gh pr create --body text, or gh pr edit --body text. Keep PR descriptions focused on the PR summary only, with no Summary header, no Details section, and no Testing section unless the user explicitly asks for them."
argument-hint: "PR title, branch, diff, or requested body update"
---

# PR Description Style

Use this skill whenever drafting, creating, or editing a GitHub pull request description for the user.

## Rule

Write the PR body as a concise summary of the PR itself. Do not add a `Summary` header; the whole body is the summary.

Do not include these sections unless the user explicitly asks for them:

- `## Summary`
- `## Details`
- `## Testing`
- validation command lists
- checklist-style boilerplate

## Preferred Shape

Use one short paragraph or a few focused bullets. Lead with what the PR changes and why it matters.

For example:

```markdown
Add link checking for documentation and other link-bearing files. PR runs check all links in changed files, including external URLs, while also checking local and relative links across all files so moved or deleted docs cannot silently break unchanged pages.
```

If the user gives a specific description, preserve their intent and only adjust structure to match this style.

## Workflow

1. Inspect the PR diff or staged changes when needed to understand the actual change.
2. Draft a body with no section header unless the user requests one.
3. Omit testing details by default, even if tests were run.
4. When editing an existing PR body, replace boilerplate sections with the concise summary format.
5. After `gh pr create` or `gh pr edit`, verify the body if practical.