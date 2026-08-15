#!/usr/bin/env bash
# Release a task's warm pool slot back to the pool. Idempotent.
#   reclaim-worktree.sh <task-id>
set -euo pipefail
TASK_ID="${1:?task-id required}"
ROOT="${TOKENDANCE_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
REPO="$(python3 "$ROOT/scripts/status.py" --root "$ROOT" get "$TASK_ID" --field repo)"
[ -n "$REPO" ] && [ "$REPO" != "None" ] || { echo "[reclaim] no repo for $TASK_ID" >&2; exit 0; }
PATH_FILE="$ROOT/state/tasks/$TASK_ID/worktree.path"
[ -f "$PATH_FILE" ] || { echo "[reclaim] no worktree.path for $TASK_ID; nothing to release" >&2; exit 0; }
WT="$(cat "$PATH_FILE")"

# A slot a live td-* tmux session still sits in must keep its worktree: release
# otherwise force-checks-out the base ref and runs `clean -fd` under that session
# (HEAD jumps off the branch the human was on, untracked files go). Mirrors the
# --busy-path collection in prepare-worktree.sh; the lease is still freed.
# Teardown kills the session first, so this never blocks a legitimate reclaim.
BUSY_ARGS=()
if command -v tmux >/dev/null 2>&1; then
  while IFS= read -r sess; do
    case "$sess" in td-*) ;; *) continue ;; esac
    while IFS= read -r p; do
      [ -n "$p" ] && BUSY_ARGS+=(--busy-path "$p")
    done < <(tmux list-panes -t "$sess" -F '#{pane_current_path}' 2>/dev/null || true)
  done < <(tmux list-sessions -F '#{session_name}' 2>/dev/null || true)
fi

python3 "$ROOT/scripts/pool.py" --root "$ROOT" release --repo "$REPO" --path "$WT" \
  --expected-holder "$TASK_ID" "${BUSY_ARGS[@]}"
echo "[reclaim] released slot for $TASK_ID ($WT)" >&2
