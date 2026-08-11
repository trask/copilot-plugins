#!/usr/bin/env python3
"""Create a validated, viewer-owned pending GitHub pull request review."""

from __future__ import annotations

import argparse
import ast
from collections import Counter
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any


PR_URL_PATTERN = re.compile(
    r"^https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>\d+)"
    r"/?(?:#\S*)?$"
)
SHORT_TARGET_PATTERN = re.compile(
    r"^(?P<owner>[^/\s]+)/(?P<repo>[^#/\s]+)#(?P<number>\d+)$"
)
HUNK_PATTERN = re.compile(
    r"^@@ -(?P<old>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new>\d+)(?:,(?P<new_count>\d+))? @@"
)
COPILOT_LOGINS = {
    "copilot-pull-request-reviewer",
    "copilot-pull-request-reviewer[bot]",
}


class WorkflowError(RuntimeError):
    pass


def run(
    command: list[str],
    *,
    input_text: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    process = subprocess.run(
        command,
        input=input_text,
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and process.returncode != 0:
        detail = process.stderr.strip() or process.stdout.strip() or "no output"
        raise WorkflowError(f"{' '.join(command)} failed ({process.returncode}): {detail}")
    return process


def emit(payload: dict[str, Any], *, stream: Any = sys.stdout) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True), file=stream, flush=True)


def gh_json(arguments: list[str], *, input_payload: Any = None) -> Any:
    input_text = None
    if input_payload is not None:
        input_text = json.dumps(input_payload, separators=(",", ":"), sort_keys=True)
    output = run(["gh", *arguments], input_text=input_text).stdout
    try:
        return json.loads(output) if output.strip() else None
    except json.JSONDecodeError as error:
        raise WorkflowError(f"gh returned invalid JSON: {error}") from error


def gh_paginated(endpoint: str) -> list[dict[str, Any]]:
    pages = gh_json(["api", "--paginate", "--slurp", endpoint])
    if not isinstance(pages, list):
        raise WorkflowError("gh pagination did not return a JSON array")
    if pages and all(isinstance(page, list) for page in pages):
        return [item for page in pages for item in page]
    if all(isinstance(item, dict) for item in pages):
        return pages
    raise WorkflowError("gh pagination returned an unexpected JSON shape")


def parse_target(target: str) -> dict[str, Any]:
    match = PR_URL_PATTERN.fullmatch(target) or SHORT_TARGET_PATTERN.fullmatch(target)
    if not match:
        raise WorkflowError("target must be a GitHub PR URL or owner/repo#number")
    values = match.groupdict()
    owner = values["owner"]
    repo = values["repo"]
    number = int(values["number"])
    return {
        "owner": owner,
        "repo": repo,
        "number": number,
        "repo_name": f"{owner}/{repo}",
        "pr_url": f"https://github.com/{owner}/{repo}/pull/{number}",
    }


def resolve_pr(target: dict[str, Any]) -> dict[str, Any]:
    metadata = gh_json(
        [
            "pr",
            "view",
            target["pr_url"],
            "--repo",
            target["repo_name"],
            "--json",
            "number,title,url,headRefOid",
        ]
    )
    if not isinstance(metadata, dict):
        raise WorkflowError("gh pr view did not return PR metadata")
    metadata_url = metadata.get("url")
    if not isinstance(metadata_url, str):
        raise WorkflowError("resolved PR metadata has no URL")
    resolved = parse_target(metadata_url)
    if (
        metadata.get("number") != target["number"]
        or resolved["repo_name"].casefold() != target["repo_name"].casefold()
    ):
        raise WorkflowError("resolved PR metadata does not match the requested target")
    head_sha = metadata.get("headRefOid")
    if not isinstance(head_sha, str) or not head_sha:
        raise WorkflowError("resolved PR metadata has no head commit")
    title = metadata.get("title")
    if not isinstance(title, str) or not title.strip():
        raise WorkflowError("resolved PR metadata has no title")
    return {**resolved, "head_sha": head_sha, "title": title.strip()}


def ensure_head_unchanged(pr: dict[str, Any], stage: str) -> None:
    expected_head = pr["head_sha"]
    current_head = resolve_pr(pr)["head_sha"]
    if current_head != expected_head:
        raise WorkflowError(
            f"PR head changed {stage}: expected {expected_head}, got {current_head}"
        )


def ensure_expected_head(pr: dict[str, Any], expected_head: str) -> None:
    current_head = pr["head_sha"]
    if current_head != expected_head:
        raise WorkflowError(
            "PR head does not match the snapshot analyzed by check: "
            f"expected {expected_head}, got {current_head}; restart from check"
        )


def resolve_viewer() -> str:
    viewer = gh_json(["api", "user"])
    login = viewer.get("login") if isinstance(viewer, dict) else None
    if not isinstance(login, str) or not login:
        raise WorkflowError("could not resolve the authenticated GitHub viewer")
    return login


def review_url(pr: dict[str, Any], review: dict[str, Any]) -> str:
    url = review.get("html_url")
    if isinstance(url, str) and url:
        return url
    review_id = review.get("id")
    if not isinstance(review_id, int):
        raise WorkflowError("review response has neither a URL nor numeric ID")
    return f"{pr['pr_url']}#pullrequestreview-{review_id}"


def fetch_reviews(pr: dict[str, Any]) -> list[dict[str, Any]]:
    return gh_paginated(
        f"repos/{pr['repo_name']}/pulls/{pr['number']}/reviews?per_page=100"
    )


def fetch_issue_comments(pr: dict[str, Any]) -> list[dict[str, Any]]:
    comments = gh_paginated(
        f"repos/{pr['repo_name']}/issues/{pr['number']}/comments?per_page=100"
    )
    normalized: list[dict[str, Any]] = []
    for index, comment in enumerate(comments):
        comment_id = comment.get("id")
        url = comment.get("html_url")
        body = comment.get("body")
        author = (comment.get("user") or {}).get("login")
        author_association = comment.get("author_association")
        created_at = comment.get("created_at")
        updated_at = comment.get("updated_at")
        if isinstance(comment_id, bool) or not isinstance(comment_id, int):
            raise WorkflowError(f"PR issue comment {index} has no numeric ID")
        if not isinstance(url, str) or not url:
            raise WorkflowError(f"PR issue comment {comment_id} has no URL")
        if not isinstance(body, str):
            raise WorkflowError(f"PR issue comment {comment_id} has no body")
        if author is not None and not isinstance(author, str):
            raise WorkflowError(f"PR issue comment {comment_id} has an invalid author")
        if not isinstance(author_association, str):
            raise WorkflowError(
                f"PR issue comment {comment_id} has no author association"
            )
        if not isinstance(created_at, str) or not isinstance(updated_at, str):
            raise WorkflowError(f"PR issue comment {comment_id} has invalid timestamps")
        normalized.append(
            {
                "id": comment_id,
                "url": url,
                "author": author,
                "author_association": author_association,
                "created_at": created_at,
                "updated_at": updated_at,
                "body": body,
            }
        )
    return normalized


def find_pending_review(
    reviews: list[dict[str, Any]], viewer: str
) -> dict[str, Any] | None:
    return next(
        (
            review
            for review in reviews
            if str(review.get("state", "")).upper() == "PENDING"
            and str((review.get("user") or {}).get("login", "")).casefold()
            == viewer.casefold()
        ),
        None,
    )


def parse_suppressed_comments(body: str | None) -> list[dict[str, Any]]:
    if not body:
        return []

    entries: list[dict[str, Any]] = []
    found_suppressed_block = False
    for details_match in re.finditer(
        r"<details\b[^>]*>(?P<body>.*?)</details\s*>",
        body,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        details = details_match.group("body")
        summary_match = re.search(
            r"<summary\b[^>]*>(?P<summary>.*?)</summary\s*>",
            details,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if not summary_match:
            continue
        summary = re.sub(r"<[^>]+>", "", summary_match.group("summary"))
        normalized_summary = " ".join(summary.split()).casefold()
        if (
            "suppressed comments" not in normalized_summary
            and "comments suppressed" not in normalized_summary
        ):
            continue
        found_suppressed_block = True
        count_match = re.search(r"\((?P<count>\d+)\)\s*$", normalized_summary)
        if not count_match:
            raise WorkflowError(
                "suppressed Copilot comments summary has no declared count"
            )

        content = details[summary_match.end() :]
        headers = list(
            re.finditer(
                r"^\s*\*\*(?P<path>.+):(?P<line>\d+)\*\*\s*$",
                content,
                flags=re.MULTILINE,
            )
        )
        block_entries: list[dict[str, Any]] = []
        for index, header in enumerate(headers):
            end = (
                headers[index + 1].start()
                if index + 1 < len(headers)
                else len(content)
            )
            path = header.group("path").strip()
            line = int(header.group("line"))
            if not path or line <= 0:
                raise WorkflowError("suppressed Copilot comment has an invalid location")
            comment_body = content[header.end() : end].strip()
            if comment_body.startswith("* "):
                comment_body = comment_body[2:].lstrip()
            if not comment_body:
                raise WorkflowError(
                    "suppressed Copilot comment has an empty body at "
                    f"{path}:{line}"
                )
            block_entries.append(
                {
                    "path": path,
                    "line": line,
                    "body": comment_body,
                }
            )

        declared_count = int(count_match.group("count"))
        if len(block_entries) != declared_count:
            raise WorkflowError(
                "suppressed Copilot comments count mismatch: "
                f"summary declares {declared_count}, parsed {len(block_entries)}"
            )
        entries.extend(block_entries)
    normalized_body = body.casefold()
    if not found_suppressed_block and (
        "suppressed comments" in normalized_body
        or "comments suppressed" in normalized_body
    ):
        raise WorkflowError(
            "suppressed Copilot comments were not in a recognized details block"
        )
    return entries


def latest_copilot_review_for_head(
    reviews: list[dict[str, Any]], head_sha: str
) -> dict[str, Any] | None:
    candidates = []
    for review in reviews:
        user = review.get("user") or {}
        login = user.get("login") if isinstance(user, dict) else None
        if not isinstance(login, str) or login.casefold() not in COPILOT_LOGINS:
            continue
        if review.get("commit_id") != head_sha or not review.get("submitted_at"):
            continue
        if str(review.get("state", "")).upper() in {"DISMISSED", "PENDING"}:
            continue
        review_id = review.get("id")
        if isinstance(review_id, bool) or not isinstance(review_id, int):
            raise WorkflowError("completed Copilot review has no numeric ID")
        candidates.append(review)
    return max(candidates, key=lambda review: int(review["id"]), default=None)


def suppressed_comments_for_head(
    reviews: list[dict[str, Any]],
    head_sha: str,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    review = latest_copilot_review_for_head(reviews, head_sha)
    if review is None:
        return None, []
    review_url_value = review.get("html_url")
    if not isinstance(review_url_value, str) or not review_url_value:
        raise WorkflowError("completed Copilot review has no URL")
    review_summary = {
        "id": review["id"],
        "url": review_url_value,
    }
    return review_summary, parse_suppressed_comments(review.get("body"))


def decode_diff_path(value: str) -> str | None:
    value = value.rstrip()
    if value == "/dev/null":
        return None
    if value.startswith('"'):
        try:
            value = ast.literal_eval(value)
        except (SyntaxError, ValueError) as error:
            raise WorkflowError(f"invalid quoted path in PR diff: {value}") from error
        try:
            value = value.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    else:
        value = value.split("\t", 1)[0]
    if value.startswith(("a/", "b/")):
        value = value[2:]
    if not value:
        raise WorkflowError("empty file path in PR diff")
    return value


def parse_unified_diff(diff_text: str) -> dict[str, dict[str, dict[int, int]]]:
    """Map changed lines to positions and all hunk lines to their hunk IDs.

    GitHub counts positions down from a file's first ``@@`` header, which itself
    is position 0, and every later line in that file counts, including
    subsequent ``@@`` headers and ``\\ No newline`` markers.
    """
    anchors: dict[str, dict[str, dict[int, int]]] = {}
    old_path: str | None = None
    new_path: str | None = None
    path: str | None = None
    old_line = new_line = 0
    old_remaining = new_remaining = 0
    position = 0
    seen_hunk = False
    in_hunk = False
    hunk_id = 0

    def finish_hunk() -> None:
        nonlocal in_hunk
        if in_hunk and (old_remaining or new_remaining):
            raise WorkflowError("PR diff ended before a hunk's declared line counts")
        in_hunk = False

    for raw_line in diff_text.split("\n"):
        raw_line = raw_line.removesuffix("\r")
        if raw_line.startswith("diff --git "):
            finish_hunk()
            old_path = new_path = path = None
            position = 0
            seen_hunk = False
            hunk_id = 0
            continue
        if not in_hunk and raw_line.startswith("--- "):
            old_path = decode_diff_path(raw_line[4:])
            continue
        if not in_hunk and raw_line.startswith("+++ "):
            new_path = decode_diff_path(raw_line[4:])
            path = new_path or old_path
            if path is None:
                raise WorkflowError("PR diff file has no usable path")
            anchors.setdefault(
                path,
                {
                    "LEFT": {},
                    "RIGHT": {},
                    "LEFT_LINES": {},
                    "RIGHT_LINES": {},
                },
            )
            continue

        hunk = HUNK_PATTERN.match(raw_line)
        if hunk:
            finish_hunk()
            if path is None:
                raise WorkflowError("PR diff hunk appeared before file headers")
            old_line = int(hunk.group("old"))
            new_line = int(hunk.group("new"))
            old_remaining = int(hunk.group("old_count") or 1)
            new_remaining = int(hunk.group("new_count") or 1)
            if seen_hunk:
                position += 1
            seen_hunk = True
            hunk_id += 1
            in_hunk = True
            continue
        if not in_hunk:
            continue
        position += 1
        if raw_line.startswith("\\"):
            continue
        if raw_line.startswith("+"):
            anchors[path]["RIGHT"].setdefault(new_line, position)
            anchors[path]["RIGHT_LINES"].setdefault(new_line, hunk_id)
            new_line += 1
            new_remaining -= 1
        elif raw_line.startswith("-"):
            anchors[path]["LEFT"].setdefault(old_line, position)
            anchors[path]["LEFT_LINES"].setdefault(old_line, hunk_id)
            old_line += 1
            old_remaining -= 1
        elif raw_line.startswith(" "):
            anchors[path]["LEFT_LINES"].setdefault(old_line, hunk_id)
            anchors[path]["RIGHT_LINES"].setdefault(new_line, hunk_id)
            old_line += 1
            new_line += 1
            old_remaining -= 1
            new_remaining -= 1
        else:
            raise WorkflowError(f"unexpected line inside PR diff hunk: {raw_line!r}")
        if old_remaining < 0 or new_remaining < 0:
            raise WorkflowError("PR diff hunk contains more lines than declared")
        if old_remaining == 0 and new_remaining == 0:
            in_hunk = False

    finish_hunk()
    return anchors


def positions_by_path(
    anchors: dict[str, dict[str, dict[int, int]]],
) -> dict[str, dict[int, tuple[str, int]]]:
    resolved: dict[str, dict[int, tuple[str, int]]] = {}
    for path, sides in anchors.items():
        for side in ("LEFT", "RIGHT"):
            lines = sides[side]
            for line, position in lines.items():
                resolved.setdefault(path, {})[position] = (side, line)
    return resolved


def fetch_authoritative_diff(pr: dict[str, Any]) -> str:
    return run(
        ["gh", "pr", "diff", pr["pr_url"], "--repo", pr["repo_name"]]
    ).stdout


def load_comments(path_value: str) -> list[dict[str, Any]]:
    try:
        text = sys.stdin.read() if path_value == "-" else Path(path_value).read_text(
            encoding="utf-8"
        )
    except OSError as error:
        raise WorkflowError(f"could not read comments JSON: {error}") from error
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise WorkflowError(f"comments are not valid JSON: {error}") from error
    if not isinstance(payload, list):
        raise WorkflowError("comments JSON must be an array")
    return payload


def validate_comments(
    comments: list[dict[str, Any]],
    anchors: dict[str, dict[str, dict[int, int]]],
) -> list[dict[str, Any]]:
    if not comments:
        raise WorkflowError("at least one inline comment is required")
    normalized: list[dict[str, Any]] = []
    required_keys = {"path", "line", "side", "body"}
    optional_keys = {"start_line", "start_side"}
    for index, comment in enumerate(comments):
        if not isinstance(comment, dict):
            raise WorkflowError(f"comment {index} must be an object")
        unknown = set(comment) - required_keys - optional_keys
        missing = required_keys - set(comment)
        if unknown or missing:
            raise WorkflowError(
                f"comment {index} must contain path, line, side, and body, "
                "with optional start_line and start_side"
            )
        path = comment["path"]
        line = comment["line"]
        side = comment["side"]
        body = comment["body"]
        has_start_line = "start_line" in comment
        has_start_side = "start_side" in comment
        if not isinstance(path, str) or not path:
            raise WorkflowError(f"comment {index} has an invalid path")
        if isinstance(line, bool) or not isinstance(line, int) or line <= 0:
            raise WorkflowError(f"comment {index} has an invalid line")
        if not isinstance(side, str) or side not in {"LEFT", "RIGHT"}:
            raise WorkflowError(f"comment {index} side must be LEFT or RIGHT")
        if not isinstance(body, str) or not body.strip():
            raise WorkflowError(f"comment {index} body must not be empty")
        if has_start_line != has_start_side:
            raise WorkflowError(
                f"comment {index} must provide start_line and start_side together"
            )
        if path not in anchors:
            raise WorkflowError(
                f"comment {index} anchor is not a changed {side} line: {path}:{line}"
            )
        if not has_start_line:
            if line not in anchors[path][side]:
                raise WorkflowError(
                    f"comment {index} anchor is not a changed {side} line: "
                    f"{path}:{line}"
                )
            normalized.append(
                {"path": path, "line": line, "side": side, "body": body}
            )
            continue

        start_line = comment["start_line"]
        start_side = comment["start_side"]
        if (
            isinstance(start_line, bool)
            or not isinstance(start_line, int)
            or start_line <= 0
        ):
            raise WorkflowError(f"comment {index} has an invalid start_line")
        if not isinstance(start_side, str) or start_side not in {"LEFT", "RIGHT"}:
            raise WorkflowError(
                f"comment {index} start_side must be LEFT or RIGHT"
            )
        if start_side != side:
            raise WorkflowError(
                f"comment {index} range must stay on the same diff side"
            )
        if start_line >= line:
            raise WorkflowError(
                f"comment {index} start_line must be less than line"
            )
        hunk_lines = anchors[path][f"{side}_LINES"]
        start_hunk = hunk_lines.get(start_line)
        end_hunk = hunk_lines.get(line)
        if start_hunk is None or end_hunk is None or start_hunk != end_hunk:
            raise WorkflowError(
                f"comment {index} range must be within one {side} diff hunk"
            )
        changed_lines = anchors[path][side]
        if not any(
            changed_line in changed_lines
            for changed_line in range(start_line, line + 1)
        ):
            raise WorkflowError(
                f"comment {index} range contains no changed {side} line"
            )
        normalized.append(
            {
                "path": path,
                "start_line": start_line,
                "start_side": start_side,
                "line": line,
                "side": side,
                "body": body,
            }
        )
    return normalized


def normalize_body(value: Any) -> str:
    """Normalize only line endings, which GitHub may rewrite in transit.

    Nothing else is normalized, because trailing spaces are Markdown hard breaks
    and leading indentation can define a code block, so stripping either could
    hide a comment that did not land as written.
    """
    return str(value).replace("\r\n", "\n").replace("\r", "\n")


def comment_signature(
    comment: dict[str, Any],
) -> tuple[str, int | None, str | None, int, str, str]:
    start_line = comment.get("start_line")
    start_side = comment.get("start_side")
    return (
        str(comment.get("path")),
        (
            start_line
            if isinstance(start_line, int) and not isinstance(start_line, bool)
            else None
        ),
        start_side if isinstance(start_side, str) else None,
        int(comment.get("line") or 0),
        str(comment.get("side")),
        normalize_body(comment.get("body")),
    )


def resolve_actual_comment(
    comment: dict[str, Any],
    positions: dict[str, dict[int, tuple[str, int]]],
) -> dict[str, Any]:
    """Fill in ``line`` and ``side`` for a comment GitHub locates only by position.

    ``GET /repos/{owner}/{repo}/pulls/{n}/reviews/{id}/comments`` returns the
    legacy comment shape, which carries ``position`` but no ``line`` or ``side``
    at all, so a review's own comments can only be located through the diff.
    """
    line = comment.get("line")
    side = comment.get("side")
    has_line_location = (
        not isinstance(line, bool)
        and isinstance(line, int)
        and isinstance(side, str)
        and side in {"LEFT", "RIGHT"}
    )
    path = str(comment.get("path"))
    if not has_line_location:
        position = comment.get("position")
        if position is None:
            position = comment.get("original_position")
        if isinstance(position, bool) or not isinstance(position, int):
            raise WorkflowError(
                f"comment on {path} reports neither a line and side nor a diff position"
            )
        resolved = positions.get(path, {}).get(position)
        if resolved is None:
            raise WorkflowError(
                f"comment on {path} has diff position {position}, "
                "which is not a changed line in the authoritative diff"
            )
        side, line = resolved

    start_line = comment.get("start_line")
    if start_line is None:
        start_line = comment.get("original_start_line")
    if start_line is None:
        return {
            **comment,
            "line": line,
            "side": side,
            "start_line": None,
            "start_side": None,
        }
    if isinstance(start_line, bool) or not isinstance(start_line, int):
        raise WorkflowError(f"comment on {path} has an invalid start line")
    start_side = comment.get("start_side")
    if not isinstance(start_side, str) or start_side not in {"LEFT", "RIGHT"}:
        start_side = side
    return {
        **comment,
        "line": line,
        "side": side,
        "start_line": start_line,
        "start_side": start_side,
    }


def verify_created_review(
    pr: dict[str, Any],
    viewer: str,
    review_id: int,
    expected_comments: list[dict[str, Any]],
    anchors: dict[str, dict[str, dict[int, int]]],
) -> dict[str, Any]:
    endpoint = f"repos/{pr['repo_name']}/pulls/{pr['number']}/reviews/{review_id}"
    review = gh_json(["api", endpoint])
    if not isinstance(review, dict):
        raise WorkflowError("created review verification returned no review")
    if review.get("commit_id") != pr["head_sha"]:
        raise WorkflowError(
            f"created review {review_id} commit does not match expected PR head "
            f"{pr['head_sha']}"
        )
    actual_viewer = str((review.get("user") or {}).get("login", ""))
    if (
        str(review.get("state", "")).upper() != "PENDING"
        or actual_viewer.casefold() != viewer.casefold()
    ):
        raise WorkflowError(
            f"created review {review_id} is not a viewer-owned PENDING review"
        )
    positions = positions_by_path(anchors)
    actual_comments = [
        resolve_actual_comment(comment, positions)
        for comment in gh_paginated(f"{endpoint}/comments?per_page=100")
    ]
    expected = Counter(comment_signature(comment) for comment in expected_comments)
    actual = Counter(comment_signature(comment) for comment in actual_comments)
    if actual != expected:
        raise WorkflowError(
            "created review inline comments failed verification: "
            f"expected {list(expected.elements())!r}, got {list(actual.elements())!r}"
        )
    ensure_head_unchanged(pr, "during final verification")
    return review


def preflight(
    target_value: str,
    expected_head: str | None = None,
    *,
    include_issue_comments: bool = False,
) -> tuple[
    dict[str, Any],
    str,
    dict[str, dict[str, dict[int, int]]],
    str | None,
    dict[str, Any] | None,
    list[dict[str, Any]],
    list[dict[str, Any]],
    str | None,
]:
    pr = resolve_pr(parse_target(target_value))
    if expected_head is not None:
        ensure_expected_head(pr, expected_head)
    viewer = resolve_viewer()
    reviews = fetch_reviews(pr)
    pending = find_pending_review(reviews, viewer)
    if pending is not None:
        return pr, viewer, {}, review_url(pr, pending), None, [], [], None
    authoritative_diff = fetch_authoritative_diff(pr)
    anchors = parse_unified_diff(authoritative_diff)
    copilot_review, suppressed_comments = suppressed_comments_for_head(
        reviews, pr["head_sha"]
    )
    issue_comments = fetch_issue_comments(pr) if include_issue_comments else []
    ensure_head_unchanged(
        pr, "after fetching the authoritative diff and review context"
    )
    return (
        pr,
        viewer,
        anchors,
        None,
        copilot_review,
        suppressed_comments,
        issue_comments,
        authoritative_diff,
    )


def command_check(args: argparse.Namespace) -> None:
    (
        pr,
        viewer,
        anchors,
        pending_url,
        copilot_review,
        suppressed_comments,
        issue_comments,
        authoritative_diff,
    ) = preflight(args.target, include_issue_comments=True)
    if pending_url:
        emit({"result": "existing_pending_review", "review_url": pending_url})
        return
    emit(
        {
            "result": "ready",
            "pr_url": pr["pr_url"],
            "pr_number": pr["number"],
            "pr_title": pr["title"],
            "head_sha": pr["head_sha"],
            "viewer": viewer,
            "changed_files": sorted(anchors),
            "authoritative_diff": authoritative_diff,
            "copilot_review": copilot_review,
            "suppressed_comments": suppressed_comments,
            "issue_comments": issue_comments,
        }
    )


def command_post(args: argparse.Namespace) -> None:
    pr, viewer, anchors, pending_url, _, _, _, _ = preflight(
        args.target, args.expected_head
    )
    ensure_expected_head(pr, args.expected_head)
    if pending_url:
        emit({"result": "existing_pending_review", "review_url": pending_url})
        return
    comments = validate_comments(load_comments(args.comments), anchors)
    payload = {"commit_id": pr["head_sha"], "comments": comments}
    endpoint = f"repos/{pr['repo_name']}/pulls/{pr['number']}/reviews"
    ensure_head_unchanged(pr, "immediately before creating the review")
    created = gh_json(
        ["api", "--method", "POST", "--input", "-", endpoint],
        input_payload=payload,
    )
    review_id = created.get("id") if isinstance(created, dict) else None
    if isinstance(review_id, bool) or not isinstance(review_id, int):
        raise WorkflowError("review creation returned no numeric review ID")
    try:
        verified = verify_created_review(pr, viewer, review_id, comments, anchors)
    except WorkflowError as error:
        created_url = review_url(pr, created)
        raise WorkflowError(
            f"review {created_url} was created but verification failed: {error}"
        ) from error
    emit(
        {
            "result": "created_pending_review",
            "review_id": review_id,
            "review_url": review_url(pr, verified),
        }
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    check = subparsers.add_parser("check", help="check for a pending review and parse the PR diff")
    check.add_argument("target")
    check.set_defaults(function=command_check)
    post = subparsers.add_parser("post", help="create and verify one pending review")
    post.add_argument("target")
    post.add_argument(
        "--expected-head",
        required=True,
        help="head SHA returned by check for the snapshot that was analyzed",
    )
    post.add_argument("--comments", required=True, help="JSON file, or - for standard input")
    post.set_defaults(function=command_post)
    return parser


def main() -> int:
    try:
        if shutil.which("gh") is None:
            raise WorkflowError("required tool not found: gh")
        args = build_parser().parse_args()
        args.function(args)
        return 0
    except WorkflowError as error:
        emit({"result": "error", "error": str(error)}, stream=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
