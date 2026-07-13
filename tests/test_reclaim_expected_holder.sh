#!/usr/bin/env bash
# reclaim-worktree.sh must pass --expected-holder so it can't free a slot whose
# lease was re-acquired by someone else (stale duplicate worktree.path). Regression
# for the pool/live-tmux collision (2026-07-13): task-A's stale worktree.path
# duplicated a slot now held by task-B; reclaiming task-A wrongly freed task-B.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; ROOT="$(dirname "$HERE")"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
REPO="$TMP/repo"; mkdir -p "$REPO"; (cd "$REPO" && git init -q -b main && \
  printf 'target/\n.venv\n' > .gitignore && echo hi > README && \
  git add -A && git -c user.email=t@t -c user.name=t commit -q -m init)
TROOT="$TMP/troot"; mkdir -p "$TROOT/scripts" "$TROOT/state/tasks/task-a"
for s in pool.py config.py status.py prepare-worktree.sh reclaim-worktree.sh; do cp "$ROOT/scripts/$s" "$TROOT/scripts/$s"; done
python3 "$TROOT/scripts/status.py" --root "$TROOT" init task-a --repo "$REPO"
python3 "$TROOT/scripts/status.py" --root "$TROOT" init task-b --repo "$REPO"
# task-b holds a live slot
WT="$("$TROOT/scripts/prepare-worktree.sh" task-b | tail -n1)"
# task-a's worktree.path staleley duplicates task-b's slot
echo "$WT" > "$TROOT/state/tasks/task-a/worktree.path"
# reclaiming task-a must NOT free task-b's lease (holder mismatch → release no-op)
"$TROOT/scripts/reclaim-worktree.sh" task-a
LINE="$(python3 "$TROOT/scripts/pool.py" --root "$TROOT" status --repo "$REPO")"
echo "$LINE" | grep -q "leased" || { echo "FAIL: task-b lease wrongly freed: $LINE"; exit 1; }
echo "$LINE" | grep -q "task-b" || { echo "FAIL: task-b no longer holder: $LINE"; exit 1; }
echo "PASS"
