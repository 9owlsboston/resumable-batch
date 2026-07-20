#!/usr/bin/env bash
# end-workstream — remove a worktree created by new-workstream.
# Usage: end-workstream <topic>
set -euo pipefail
TOPIC="${1:?usage: end-workstream <topic>}"
ROOT="$(git rev-parse --show-toplevel)"
NAME="$(basename "$ROOT")"
WT="$(dirname "$ROOT")/${NAME}.worktrees/${TOPIC}"
git -C "$ROOT" worktree remove "$WT" --force
git -C "$ROOT" worktree prune
echo "Removed worktree $WT."
echo "After the PR is merged, delete the branch:  git branch -d <branch>"
