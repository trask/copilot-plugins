---
name: pr-file-copy-diff-annotation
description: Annotate PR files with git diffs showing changes from their original source files. Use this when asked to annotate a PR with diffs between new/renamed files and the old files they were copied from.
---

# PR File Copy Diff Annotation

This skill helps annotate pull request files with review comments showing the exact git diff between new files and the original files they were copied/renamed from.

## When to Use

Use this skill when the user wants to:
- Annotate a PR with comments showing how files were modified from their original sources
- Show diffs between renamed/copied files and their origins
- Show diffs between vendored files and their external upstream sources
- Document file provenance in PR review comments

## Prerequisites

- The PR must be in a git repository with `upstream/main` (or the appropriate default branch) available
- The `gh` CLI must be authenticated
- Old files must still exist on the default branch

## Process

### Step 1: Discover File Mappings Automatically

Do NOT ask the user for file mappings. Instead, discover them from the PR automatically.

**1a. Get the changed files:**
```bash
git diff --name-status upstream/main...HEAD
```

**1b. Extract renamed files (`R` status):**
Git already knows both sides of a rename. Lines beginning with `R` have the format:
```
R<score>  <old-path>  <new-path>
```
These map directly: `new-path -> old-path`.

**1c. Find sources for added files (`A` status):**
For each added file, search for likely source files by:
- Looking for files with the same base filename but under a different versioned directory (e.g. `quarkus-3.0-testing` → `quarkus-3.9-testing`)
- Using `git log --diff-filter=R --summary upstream/main...HEAD` to detect renames git may have missed
- Comparing directory structure patterns within the same module

**1d. Identify vendored files:**
Some added files may be vendored from external upstream projects rather than copied from within the repo. Check for:
- `// Based on <url>` comments in the source files
- Directory naming patterns suggesting external origins (e.g. vendored framework code)
- User context about vendoring relationships

For vendored files, map them to their external upstream URL instead of an in-repo source.

**1e. Avoid duplicate annotations:** If a file already has an upstream vendor diff, do NOT also generate an old-version copy diff for the same file. Each file should get at most one annotation.

**1f. Skip files with no plausible source** (e.g. brand-new files with unique names). Only annotate files that were clearly copied or adapted from an existing file.

**1g. Confirm the discovered mappings** by briefly listing them to the user before proceeding.

### Step 2: Generate Git Diffs

**For in-repo copy/rename mappings:**
```bash
git diff upstream/main:<old-path> <new-path>
```

**For vendored files (external upstream sources):**
```bash
curl -sL <upstream-raw-url> -o /tmp/upstream_file
git diff --no-index --ignore-all-space -- /tmp/upstream_file <local-path>
```
Use `--ignore-all-space` for upstream diffs since whitespace differences are usually not meaningful.

### Step 3: Format Review Comments

**For in-repo copies:**
~~~
Copied from `<old-dir>/<OldFileName>`

````diff
<diff output without the "diff --git" header line>
````
~~~

**For vendored upstream files:**
~~~
Vendored from [upstream ProjectName version](<browseable-github-url>)
Diff generated with `--ignore-all-space`

````diff
<diff output without the "diff --git" header line>
````
~~~

For upstream files, link to the browseable GitHub URL (e.g. `https://github.com/org/repo/blob/tag/path/File.java`), not the raw URL.

**Important:** Use 4 backticks for diff blocks to prevent embedded 3-backtick code blocks (like ```java or ```groovy) from breaking the markdown rendering.

### Step 4: Use Python for JSON Generation

Shell heredocs are unreliable for diffs containing backticks, quotes, and special characters. **Always use a Python script** to build the JSON payload:

```python
#!/usr/bin/env python3
import json, subprocess, os

def run(cmd, cwd=None):
    return subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)

# ... build comments list, each with: path, body, position
# position=1 places the comment on the first line of the file's first diff hunk
review = {
    "commit_id": COMMIT_SHA,
    "body": "File copy/rename annotations with diffs",
    "comments": [{"path": p, "position": 1, "body": b} for p, b in comments]
}
with open("/tmp/review.json", "w") as f:
    json.dump(review, f, ensure_ascii=False)
```

### Step 5: Post as a Pending Draft Review

Post the JSON payload using the GitHub API. Omitting the `event` field leaves the review pending.

```bash
gh api repos/<owner>/<repo>/pulls/<pr-number>/reviews --method POST --input /tmp/review.json
```

## Important Notes

1. **Rename detection first**: Always check `R`-status entries before searching for added-file sources — they are the most reliable mappings.
2. **Get the commit SHA**: Use `git rev-parse HEAD` for the review's `commit_id`. Ensure it matches the PR's HEAD on GitHub.
3. **Use Python for JSON**: Never use shell heredocs for JSON with diff content — Python's `json.dump()` handles all escaping automatically.
4. **Use `position: 1` on the batch endpoint**: The batch review endpoint (`POST /pulls/{pr}/reviews`) uses `position` (diff offset from the first `@@` hunk header), not `line`/`side`. `position: 1` always works — it targets the first line of the first diff hunk for each file. The `line`/`side` parameters are only for the individual comment endpoint (`POST /pulls/{pr}/comments`). Note: `line: null` in API responses for batch-posted comments is normal and does not mean the comment is broken.
5. **Remove diff header**: Strip the `diff --git a/... b/...` line, keeping only from `--- a/` onward.
6. **Draft stays private**: The pending review is only visible to you until you submit it on GitHub.
7. **Large files are collapsed**: GitHub doesn't auto-expand large files in the diff view. Comments on those files exist but won't be visible until the user clicks "Load diff" on that file. This is expected, not a bug.
8. **`subject_type: "file"` only works on the individual comment endpoint** (`POST /pulls/{pr}/comments`), NOT on the batch review endpoint (`POST /pulls/{pr}/reviews`). Do not use it in batch review payloads.
10. **One annotation per file**: Never give a file both an upstream vendor diff and an old-version copy diff. Upstream diffs take priority.

## Example Workflow

```bash
# Step 1 — discover mappings
git diff --name-status upstream/main...HEAD
# -> parses R-lines for renames; infers sources for A-lines by filename pattern

# Step 2 — generate diffs (done inside the Python script)
# In-repo:  git diff upstream/main:<old-path> <new-path>
# Upstream: git diff --no-index --ignore-all-space -- /tmp/upstream <local>

# Step 3 — build JSON via Python, post pending draft review
python /tmp/build_review.py
# The script generates /tmp/review.json and posts it via gh api
```

## Troubleshooting

- **"fatal: path 'X' does not exist"**: The old file path is incorrect or doesn't exist on `upstream/main`
- **Review not appearing on GitHub**: Pending reviews are only visible to you until submitted; go to the PR → "Pending review" → **Submit review**
- **Review not matching PR head**: Ensure `commit_id` from `git rev-parse HEAD` matches the PR's latest commit (check with `gh api repos/.../pulls/<N> --jq '.head.sha'`)
- **Comments invisible on large files**: GitHub collapses large file diffs. The comments are there — click "Load diff" on the file to see them.
- **No source found for an added file**: Skip it — only annotate files with a clear copy origin
- **422 error posting review**: Check that all comment `path` values match filenames in the PR diff exactly, and that `line`/`side` point to lines that exist in the diff
