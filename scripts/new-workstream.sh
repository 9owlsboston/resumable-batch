#!/usr/bin/env bash
# new-workstream — create an isolated git worktree for one workstream.
# One terminal <-> one worktree <-> one branch <-> one PR.
# Bases off origin/<default> (fetched first) so you never branch off a stale anchor.
#
# Usage: new-workstream <branch> [base-ref]
#   new-workstream fix/login-redirect
#   new-workstream feat/x origin/main
set -euo pipefail

BRANCH="${1:?usage: new-workstream <branch> [base-ref]}"
BASE="${2:-}"

ROOT="$(git rev-parse --show-toplevel)"
NAME="$(basename "$ROOT")"
TOPIC="${BRANCH##*/}"
WT="$(dirname "$ROOT")/${NAME}.worktrees/${TOPIC}"

git -C "$ROOT" fetch origin --quiet
if [ -z "$BASE" ]; then
  DEF="$(git -C "$ROOT" symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null | sed 's@^origin/@@')"
  DEF="${DEF:-main}"
  BASE="origin/${DEF}"
fi

[ -e "$WT" ] && { echo "worktree path already exists: $WT" >&2; exit 1; }
mkdir -p "$(dirname "$WT")"
git -C "$ROOT" worktree add "$WT" -b "$BRANCH" "$BASE"

# Share JS deps from the main checkout (same OS/arch) via symlink, if present.
while IFS= read -r pj; do
  d="$(dirname "$pj")"
  src="$ROOT/$d/node_modules"; dst="$WT/$d/node_modules"
  if [ -d "$src" ] && [ ! -e "$dst" ]; then ln -s "$src" "$dst"; echo "  symlinked $d/node_modules"; fi
done < <(cd "$ROOT" && git ls-files '*package.json' 'package.json' 2>/dev/null | grep -v node_modules || true)

PORT=$((3001 + $(printf '%s' "$TOPIC" | cksum | cut -d' ' -f1) % 999))
cat <<EOF

Worktree ready: $WT
  branch: $BRANCH  (off $BASE)

Next steps (in a DEDICATED terminal):
  cd "$WT"
  export PORT=$PORT          # deterministic per-topic port, if you run a dev server
  # then launch your agent / editor here

When merged:  end-workstream $TOPIC
EOF
