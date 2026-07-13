#!/usr/bin/env bash
# Worker isolation via warm worktree pool.
#
#   prepare-worktree.sh <task-id>
#
# Behaviour:
#   1. Reads the target repo from status.json via status.py.
#   2. Leases a warm pool slot via `pool.py acquire` (creates or reuses a slot
#      under state/pool/<repo-key>/<n>/; resets to HEAD; applies shared-symlink
#      list — defaulting to .venv — inside acquire).
#      Per-slot target/ is preserved across leases for warm build caches;
#      target/ is NEVER symlinked here.
#   3. Records --branch tokendance/<id> in status.json and writes the path to
#      state/tasks/<id>/worktree.path for reclaim/debug visibility.
#
# Output: last stdout line = worktree absolute path.  Diagnostics to stderr.
#         launch-worker.sh captures that path as the worker's cwd.
#
# Reclaim:
#   Call `python3 scripts/pool.py --root <ROOT> release --repo <repo> --path <wt>`
#   once the task reaches done/failed.  The slot is reset and returned to the
#   idle pool for the next task.
set -euo pipefail
TASK_ID="${1:?task-id required}"
ROOT="${TOKENDANCE_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
log() { echo "[prepare-worktree] $*" >&2; }

REPO="$(python3 "$ROOT/scripts/status.py" --root "$ROOT" get "$TASK_ID" --field repo)"
if [ -z "$REPO" ] || [ "$REPO" = "None" ]; then
  log "task $TASK_ID has no repo — cannot isolate"; exit 1
fi
REPO="$(cd "$REPO" && pwd)"

# Slots occupied by a live human-driven session (td-* tmux window) must not be
# reused — git worktree allows one checkout per slot, and overwriting a live
# session's cwd corrupts it. Collect each live td-* session's pane cwd and pass
# it as --busy-path so acquire skips those slots. Best-effort: tmux missing / no
# server → empty set (errors ignored). pool.py stays tmux-free; this is the only
# tmux dependency.
BUSY_ARGS=()
if command -v tmux >/dev/null 2>&1; then
  while IFS= read -r sess; do
    case "$sess" in td-*) ;; *) continue ;; esac
    while IFS= read -r p; do
      [ -n "$p" ] && BUSY_ARGS+=(--busy-path "$p")
    done < <(tmux list-panes -t "$sess" -F '#{pane_current_path}' 2>/dev/null || true)
  done < <(tmux list-sessions -F '#{session_name}' 2>/dev/null || true)
fi

# Lease a warm pool slot (per-slot target/, shared .venv symlink applied inside).
WT="$(python3 "$ROOT/scripts/pool.py" --root "$ROOT" acquire --repo "$REPO" --holder "$TASK_ID" "${BUSY_ARGS[@]}")"
[ -d "$WT" ] || { log "pool acquire returned no dir"; exit 1; }

# Record branch + path for visibility/reclaim (unchanged contract).
python3 "$ROOT/scripts/status.py" --root "$ROOT" set "$TASK_ID" --branch "tokendance/$TASK_ID" >/dev/null
mkdir -p "$ROOT/state/tasks/$TASK_ID"
echo "$WT" > "$ROOT/state/tasks/$TASK_ID/worktree.path"
echo "$WT"
