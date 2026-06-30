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

# Lease a warm pool slot (per-slot target/, shared .venv symlink applied inside).
WT="$(python3 "$ROOT/scripts/pool.py" --root "$ROOT" acquire --repo "$REPO" --holder "$TASK_ID")"
[ -d "$WT" ] || { log "pool acquire returned no dir"; exit 1; }

# Record branch + path for visibility/reclaim (unchanged contract).
python3 "$ROOT/scripts/status.py" --root "$ROOT" set "$TASK_ID" --branch "tokendance/$TASK_ID" >/dev/null
echo "$WT" > "$ROOT/state/tasks/$TASK_ID/worktree.path"
echo "$WT"
