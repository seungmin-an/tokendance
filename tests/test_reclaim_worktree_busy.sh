#!/usr/bin/env bash
# reclaim-worktree.sh must not reset a worktree a live td-* tmux session occupies:
# it collects each live session's pane cwd and passes them as --busy-path to
# pool.py release, which then frees the lease but leaves the tree alone.
# Regression for the 2026-07-19 "점유중인 세션의 HEAD 가 origin/master 로 튀는" report.
# tmux is stubbed to report one td-* session sitting on the leased slot.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; ROOT="$(dirname "$HERE")"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
REPO="$TMP/repo"; mkdir -p "$REPO"; (cd "$REPO" && git init -q -b main && \
  printf 'target/\n.venv\n' > .gitignore && echo hi > README && \
  git add -A && git -c user.email=t@t -c user.name=t commit -q -m init)
TROOT="$TMP/troot"; mkdir -p "$TROOT/scripts"
for s in pool.py config.py status.py prepare-worktree.sh reclaim-worktree.sh; do
  cp "$ROOT/scripts/$s" "$TROOT/scripts/$s"
done
python3 "$TROOT/scripts/status.py" --root "$TROOT" init task-1 --repo "$REPO" >/dev/null

WT="$("$TROOT/scripts/prepare-worktree.sh" task-1 | tail -n1)"
# the human drives the session onto their own branch and leaves untracked work
git -C "$WT" checkout -q -B mywork
echo "work in progress" > "$WT/scratch.txt"

STUB="$TMP/bin"; mkdir -p "$STUB"
cat > "$STUB/tmux" <<STUBEOF
#!/usr/bin/env bash
case "\$1" in
  list-sessions) echo "td-task-1" ;;
  list-panes) echo "$WT" ;;
  *) exit 0 ;;
esac
STUBEOF
chmod +x "$STUB/tmux"

PATH="$STUB:$PATH" TOKENDANCE_ROOT="$TROOT" "$TROOT/scripts/reclaim-worktree.sh" task-1 >/dev/null 2>&1

HEAD_NOW="$(git -C "$WT" rev-parse --abbrev-ref HEAD)"
[ "$HEAD_NOW" = "mywork" ] || { echo "FAIL: HEAD moved to $HEAD_NOW under a live session"; exit 1; }
[ -f "$WT/scratch.txt" ] || { echo "FAIL: untracked work was cleaned away"; exit 1; }
LEASED="$(python3 -c "
import json,sys
s=json.load(open('$TROOT/state/pool/'+[d for d in __import__('os').listdir('$TROOT/state/pool')][0]+'/state.json'))
print(s['entries'][0]['leased'])")"
[ "$LEASED" = "False" ] || { echo "FAIL: lease not freed (leased=$LEASED)"; exit 1; }
echo "PASS"
