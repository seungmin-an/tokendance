#!/usr/bin/env bash
# reclaim must delete the slot's target/ build cache (사용자 지시 2026-09-01
# "앞으로 reclaim 할 때 target dir 도 지워"). reset_worktree deliberately runs
# `clean -fd` without -x, so gitignored target/ used to survive a reclaim and a
# single npu-tools slot grew to 115G. The delete belongs in release(), not in
# reset_worktree: acquire() also calls reset_worktree and must keep reusing the
# cache it just leased.
#
# Second case: a slot a live session still occupies keeps its target/ — the same
# guard that stops us resetting a human's tree must stop us deleting their cache
# mid-build.
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

# --- case 1: plain reclaim deletes target/ ---------------------------------
python3 "$TROOT/scripts/status.py" --root "$TROOT" init task-1 --repo "$REPO" >/dev/null
WT="$("$TROOT/scripts/prepare-worktree.sh" task-1 | tail -n1)"
mkdir -p "$WT/target/debug"; echo build-cache > "$WT/target/debug/artifact.o"
[ -f "$WT/target/debug/artifact.o" ] || { echo "FAIL: setup — target/ not created"; exit 1; }
"$TROOT/scripts/reclaim-worktree.sh" task-1 >/dev/null
[ -d "$WT/target" ] && { echo "FAIL: target/ survived reclaim"; exit 1; }
[ -f "$WT/README" ] || { echo "FAIL: reclaim removed tracked content"; exit 1; }

# --- case 2: a live session's target/ is left alone ------------------------
python3 "$TROOT/scripts/status.py" --root "$TROOT" init task-2 --repo "$REPO" >/dev/null
WT2="$("$TROOT/scripts/prepare-worktree.sh" task-2 | tail -n1)"
mkdir -p "$WT2/target/debug"; echo mid-build > "$WT2/target/debug/artifact.o"
STUB="$TMP/bin"; mkdir -p "$STUB"
cat > "$STUB/tmux" <<STUBEOF
#!/usr/bin/env bash
case "\$1" in
  list-sessions) echo "td-task-2" ;;
  list-panes) echo "$WT2" ;;
  *) exit 0 ;;
esac
STUBEOF
chmod +x "$STUB/tmux"
PATH="$STUB:$PATH" TOKENDANCE_ROOT="$TROOT" "$TROOT/scripts/reclaim-worktree.sh" task-2 >/dev/null 2>&1
[ -f "$WT2/target/debug/artifact.o" ] || { echo "FAIL: deleted target/ out from under a live session"; exit 1; }

echo "PASS"
