#!/usr/bin/env bash
#
# Links this repo's Copilot configuration into ~/.copilot.
#
#   ./link.sh          link directories and copy loose files into ~/.copilot
#   ./link.sh pull     copy loose files from ~/.copilot back into this repo
#
# On Windows (Git Bash / MSYS) directories are linked with NTFS junctions, which
# do not require administrator rights the way symlinks do. Elsewhere plain
# symlinks are used.

set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
copilot="$HOME/.copilot"

# Backups live outside ~/.copilot on purpose: a leftover copy under skills/ or
# agents/ would be scanned and loaded as a duplicate skill/agent.
backup_root="$HOME/.copilot-backups/$(date +%Y%m%d-%H%M%S)"

# Directories linked from repo -> ~/.copilot
linked_dirs=(
  instructions
  agents
  skills/github-pr-diff-review
  skills/pr-description-style
  skills/pr-file-copy-diff-annotation
)

# Files copied in both directions (a link can't survive a git checkout)
copied_files=(
  copilot-instructions.md
  settings.json
)

case "$(uname -s)" in
  MINGW* | MSYS* | CYGWIN*) is_windows=1 ;;
  *) is_windows=0 ;;
esac

back_up() {
  local src="$1"
  local rel="$2"
  local dest="$backup_root/$rel"
  mkdir -p "$(dirname "$dest")"
  mv "$src" "$dest"
  echo "backup  $rel -> $dest"
}

make_link() {
  local target="$1"
  local link="$2"
  if [ "$is_windows" -eq 1 ]; then
    # MSYS_NO_PATHCONV keeps the /c and /J switches from being mangled into
    # paths; cmd needs single-slash switches and native Windows arguments.
    MSYS_NO_PATHCONV=1 cmd /c mklink /J \
      "$(cygpath -w "$link")" "$(cygpath -w "$target")" >/dev/null
  else
    ln -s "$target" "$link"
  fi
  if ! link_matches "$target" "$link"; then
    echo "failed to link $link -> $target" >&2
    exit 1
  fi
}

link_matches() {
  local target="$1"
  local link="$2"
  if [ "$is_windows" -eq 1 ]; then
    # A junction reports as a directory, so compare the resolved paths instead.
    [ -d "$link" ] && [ "$(cd "$link" && pwd -P)" = "$(cd "$target" && pwd -P)" ]
  else
    [ -L "$link" ] && [ "$(readlink "$link")" = "$target" ]
  fi
}

if [ "${1:-}" = "pull" ]; then
  for file in "${copied_files[@]}"; do
    if [ -f "$copilot/$file" ]; then
      cp "$copilot/$file" "$repo/$file"
      echo "pulled  $file"
    fi
  done
  echo
  echo "Done. Review with 'git diff' and commit."
  exit 0
fi

if [ ! -d "$copilot" ]; then
  echo "Copilot config directory not found: $copilot" >&2
  exit 1
fi

for dir in "${linked_dirs[@]}"; do
  target="$repo/$dir"
  link="$copilot/$dir"

  if [ ! -d "$target" ]; then
    echo "skipped $dir (not present in repo)"
    continue
  fi

  if link_matches "$target" "$link"; then
    echo "ok      $dir (already linked)"
    continue
  fi

  if [ -L "$link" ]; then
    rm "$link"
  elif [ -e "$link" ]; then
    back_up "$link" "$dir"
  fi

  mkdir -p "$(dirname "$link")"
  make_link "$target" "$link"
  echo "linked  $dir"
done

for file in "${copied_files[@]}"; do
  src="$repo/$file"
  dst="$copilot/$file"

  if [ ! -f "$src" ]; then
    echo "skipped $file (not present in repo)"
    continue
  fi

  if [ -f "$dst" ] && ! cmp -s "$src" "$dst"; then
    mkdir -p "$(dirname "$backup_root/$file")"
    cp "$dst" "$backup_root/$file"
    echo "backup  $file -> $backup_root/$file"
  fi

  cp "$src" "$dst"
  echo "copied  $file"
done

echo
echo "Done. Restart Copilot to pick up the changes."
echo "Plugins listed in settings.json reinstall themselves from their marketplace."
