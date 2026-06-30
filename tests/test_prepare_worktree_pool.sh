#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; ROOT="$(dirname "$HERE")"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
# a target repo
REPO="$TMP/repo"; mkdir -p "$REPO"; (cd "$REPO" && git init -q -b main && \
  printf 'target/\n.venv\n' > .gitignore && echo hi > README && \
  git add -A && git -c user.email=t@t -c user.name=t commit -q -m init)
mkdir -p "$REPO/.venv"
# a task that points at REPO (use a throwaway tokendance ROOT = TMP/ROOT with scripts symlinked)
TROOT="$TMP/troot"; mkdir -p "$TROOT/scripts" "$TROOT/state/tasks/task-1"
for s in pool.py config.py status.py prepare-worktree.sh; do cp "$ROOT/scripts/$s" "$TROOT/scripts/$s"; done
python3 "$TROOT/scripts/status.py" --root "$TROOT" init task-1 --repo "$REPO"   # init sets state=queued
OUT="$("$TROOT/scripts/prepare-worktree.sh" task-1)"
WT="$(echo "$OUT" | tail -n1)"
[ -d "$WT" ] || { echo "FAIL: worktree dir missing: $WT"; exit 1; }
[ -L "$WT/.venv" ] || { echo "FAIL: .venv not symlinked"; exit 1; }
[ "$(git -C "$WT" rev-parse --abbrev-ref HEAD)" = "tokendance/task-1" ] || { echo "FAIL: wrong branch"; exit 1; }
echo "PASS"
