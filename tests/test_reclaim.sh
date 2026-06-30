#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; ROOT="$(dirname "$HERE")"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
REPO="$TMP/repo"; mkdir -p "$REPO"; (cd "$REPO" && git init -q -b main && \
  printf 'target/\n.venv\n' > .gitignore && echo hi > README && \
  git add -A && git -c user.email=t@t -c user.name=t commit -q -m init)
TROOT="$TMP/troot"; mkdir -p "$TROOT/scripts" "$TROOT/state/tasks/task-1"
for s in pool.py config.py status.py prepare-worktree.sh reclaim-worktree.sh; do cp "$ROOT/scripts/$s" "$TROOT/scripts/$s"; done
python3 "$TROOT/scripts/status.py" --root "$TROOT" init task-1 --repo "$REPO"   # init sets state=queued
WT="$("$TROOT/scripts/prepare-worktree.sh" task-1 | tail -n1)"
"$TROOT/scripts/reclaim-worktree.sh" task-1
LINE="$(python3 "$TROOT/scripts/pool.py" --root "$TROOT" status --repo "$REPO")"
echo "$LINE" | grep -q idle || { echo "FAIL: slot not released: $LINE"; exit 1; }
"$TROOT/scripts/reclaim-worktree.sh" task-1   # idempotent second call must not error
echo "PASS"
