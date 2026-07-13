#!/usr/bin/env bash
# prepare-worktree.sh must not lease a slot a live td-* tmux session occupies:
# it collects each live td-* session's window cwd and passes them as --busy-path
# to pool.py acquire. Regression for the 2026-07-13 live-tmux worktree collision.
# tmux is stubbed to report one td-* session sitting on the idle slot.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; ROOT="$(dirname "$HERE")"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
REPO="$TMP/repo"; mkdir -p "$REPO"; (cd "$REPO" && git init -q -b main && \
  printf 'target/\n.venv\n' > .gitignore && echo hi > README && \
  git add -A && git -c user.email=t@t -c user.name=t commit -q -m init)
TROOT="$TMP/troot"; mkdir -p "$TROOT/scripts"
for s in pool.py config.py status.py prepare-worktree.sh; do cp "$ROOT/scripts/$s" "$TROOT/scripts/$s"; done
python3 "$TROOT/scripts/status.py" --root "$TROOT" init task-1 --repo "$REPO" >/dev/null
python3 "$TROOT/scripts/status.py" --root "$TROOT" init task-2 --repo "$REPO" >/dev/null

# task-1 leases then releases a slot → it becomes the idle candidate for reuse
BUSY="$("$TROOT/scripts/prepare-worktree.sh" task-1 | tail -n1)"
python3 "$TROOT/scripts/pool.py" --root "$TROOT" release --repo "$REPO" --path "$BUSY"

# stub tmux: one live session td-fake whose pane cwd is the idle slot
STUB="$TMP/bin"; mkdir -p "$STUB"
cat > "$STUB/tmux" <<STUBEOF
#!/usr/bin/env bash
case "\$1" in
  list-sessions) echo "td-fake" ;;
  list-panes) echo "$BUSY" ;;
  *) exit 0 ;;
esac
STUBEOF
chmod +x "$STUB/tmux"

# prepare for task-2 must skip the busy slot and create a fresh one
WT="$(PATH="$STUB:$PATH" "$TROOT/scripts/prepare-worktree.sh" task-2 | tail -n1)"
[ -d "$WT" ] || { echo "FAIL: no worktree: $WT"; exit 1; }
[ "$WT" != "$BUSY" ] || { echo "FAIL: reused busy slot $BUSY"; exit 1; }
echo "PASS"
