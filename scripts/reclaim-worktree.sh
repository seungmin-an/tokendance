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
python3 "$ROOT/scripts/pool.py" --root "$ROOT" release --repo "$REPO" --path "$WT"
echo "[reclaim] released slot for $TASK_ID ($WT)" >&2
