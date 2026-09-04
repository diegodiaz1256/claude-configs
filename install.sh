#!/usr/bin/env bash
# Install the statusline into ~/.claude and point settings.json at it.
#
#   ./install.sh          symlink (edits in this repo take effect immediately)
#   ./install.sh --copy   copy instead
#
# The existing settings.json is backed up before it is touched. Nothing else in
# it is modified — only the statusLine key.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
HOOKS_DIR="$CLAUDE_DIR/hooks"
SETTINGS="$CLAUDE_DIR/settings.json"
TARGET="$HOOKS_DIR/statusline.py"

MODE="symlink"
[ "${1:-}" = "--copy" ] && MODE="copy"

command -v python3 >/dev/null || { echo "error: python3 not found" >&2; exit 1; }

mkdir -p "$HOOKS_DIR"

SOURCE="$REPO/hooks/statusline.py"

if [ "$MODE" = "symlink" ]; then
  ln -sfn "$SOURCE" "$TARGET"
  echo "linked  $TARGET -> $SOURCE"
else
  # Switching from a previous symlink install would otherwise make cp refuse to
  # copy the file onto itself, and -e would abort before the backup below.
  [ -L "$TARGET" ] && rm -f "$TARGET"
  if [ "$(readlink -f "$SOURCE")" = "$(readlink -f "$TARGET" 2>/dev/null)" ]; then
    echo "in place $TARGET (already this file)"
  else
    cp "$SOURCE" "$TARGET"
    echo "copied  $TARGET"
  fi
fi

if [ ! -f "$SETTINGS" ]; then
  # No settings yet: write the minimum that makes the statusline work rather
  # than dropping this repo's machine-specific reference copy on them.
  mkdir -p "$CLAUDE_DIR"
  cat > "$SETTINGS" <<JSON
{
  "statusLine": {
    "type": "command",
    "command": "python3 $TARGET"
  }
}
JSON
  echo "created $SETTINGS"
else
  BACKUP="$SETTINGS.bak.$(date +%Y%m%d-%H%M%S)"
  cp "$SETTINGS" "$BACKUP"
  echo "backup  $BACKUP"

  # Rewrite only statusLine, preserving key order and everything else. Bail
  # without saving if the file is not valid JSON.
  TARGET="$TARGET" SETTINGS="$SETTINGS" python3 <<'PY'
import json, os, sys

path = os.environ["SETTINGS"]
try:
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
except (json.JSONDecodeError, ValueError) as exc:
    sys.exit(f"error: {path} is not valid JSON ({exc}); left unchanged")

cfg["statusLine"] = {
    "type": "command",
    "command": f"python3 {os.environ['TARGET']}",
}
with open(path, "w", encoding="utf-8") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")
print(f"updated {path} statusLine")
PY
fi

echo
echo "Preview:"
printf '%s' '{"model":{"display_name":"Opus"},"effort":{"level":"medium"},
 "context_window":{"context_window_size":200000,"used_percentage":72},
 "cost":{"total_lines_added":156,"total_lines_removed":23}}' \
  | python3 "$TARGET" || true
echo
echo
echo "Needs a Nerd Font for the glyphs; CLAUDE_STATUSLINE_ASCII=1 falls back to text."
