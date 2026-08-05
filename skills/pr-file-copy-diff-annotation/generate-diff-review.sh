#!/usr/bin/env bash
# generate-diff-review.sh
# Generates a GitHub review JSON payload with file copy diff annotations
#
# Usage: ./generate-diff-review.sh <pr-number> <owner/repo> <file-mappings.txt>
#
# file-mappings.txt format (one mapping per line):
#   new-file-path|old-file-path
#
# Example:
#   src/new/Service.java|src/old/PeerService.java

set -e

PR_NUMBER="$1"
REPO="$2"
MAPPINGS_FILE="$3"

if [[ -z "$PR_NUMBER" || -z "$REPO" || -z "$MAPPINGS_FILE" ]]; then
  echo "Usage: $0 <pr-number> <owner/repo> <file-mappings.txt>" >&2
  exit 1
fi

if [[ ! -f "$MAPPINGS_FILE" ]]; then
  echo "Error: Mappings file not found: $MAPPINGS_FILE" >&2
  exit 1
fi

# Get current commit SHA
COMMIT_SHA=$(git rev-parse HEAD)

# Function to escape string for JSON
json_escape() {
  python3 -c 'import json,sys; print(json.dumps(sys.stdin.read())[1:-1])'
}

# Start building JSON
echo "{"
echo "  \"commit_id\": \"$COMMIT_SHA\","
echo "  \"body\": \"File copy/rename annotations with diffs\","
echo "  \"event\": \"COMMENT\","
echo "  \"comments\": ["

FIRST=true
while IFS='|' read -r NEW_FILE OLD_FILE || [[ -n "$NEW_FILE" ]]; do
  # Skip empty lines and comments
  [[ -z "$NEW_FILE" || "$NEW_FILE" =~ ^# ]] && continue
  
  # Trim whitespace
  NEW_FILE=$(echo "$NEW_FILE" | xargs)
  OLD_FILE=$(echo "$OLD_FILE" | xargs)
  
  # Get just the filename for the "Copied from" message
  OLD_FILENAME=$(basename "$OLD_FILE")
  
  # Generate the diff (skip the "diff --git" line)
  DIFF_CMD="git diff upstream/main:$OLD_FILE $NEW_FILE"
  DIFF_OUTPUT=$($DIFF_CMD 2>/dev/null | tail -n +2) || {
    echo "Warning: Could not generate diff for $NEW_FILE from $OLD_FILE" >&2
    continue
  }
  
  # Build the comment body
  COMMENT_BODY="Copied from $OLD_FILENAME

\`\`\`diff
$DIFF_CMD
$DIFF_OUTPUT
\`\`\`"

  # Escape for JSON
  ESCAPED_BODY=$(echo "$COMMENT_BODY" | json_escape)
  
  # Add comma before all but first comment
  if [[ "$FIRST" == "true" ]]; then
    FIRST=false
  else
    echo ","
  fi
  
  # Output the comment JSON
  echo "    {"
  echo "      \"path\": \"$NEW_FILE\","
  echo "      \"line\": 1,"
  echo "      \"side\": \"RIGHT\","
  echo "      \"body\": \"$ESCAPED_BODY\""
  echo -n "    }"
  
done < "$MAPPINGS_FILE"

echo ""
echo "  ]"
echo "}"
