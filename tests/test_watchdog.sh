#!/usr/bin/env bash
# watchdog 통합 테스트(실제 supervisor.py/claude 미기동 — TOKENDANCE_SUPERVISOR_CMD seam):
#   1) supervisor 죽어 있으면 워치독이 start.sh 로 되살린다.
#   2) 이미 살아 있으면 아무것도 하지 않는다.
#   3) stop.sh 의 의도적 정지 마커가 있으면 되살리지 않는다.
#   4) start.sh 는 그 마커를 지운다(감시 재개).
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"

cleanup() {
  [ -f "$WORK/state/supervisor.pid" ] && kill -- -"$(cat "$WORK/state/supervisor.pid")" 2>/dev/null
  rm -rf "$WORK"
}
trap cleanup EXIT

mkdir -p "$WORK/scripts" "$WORK/state"
cp "$ROOT/scripts/start.sh" "$ROOT/scripts/stop.sh" "$ROOT/scripts/supervise.sh" "$WORK/scripts/"
export TOKENDANCE_CLAUDE=/bin/true
export TOKENDANCE_SUPERVISOR_CMD='sleep 300'   # 가짜 감시대상

FAIL=0
check() {  # check <설명> <기대> <실제>
  if [ "$2" = "$3" ]; then echo "  ok   $1"; else echo "  FAIL $1: 기대=$2 실제=$3"; FAIL=1; fi
}
wd() { python3 "$ROOT/scripts/watchdog.py" --root "$WORK" --once; }

echo "[1] supervisor 없음 → 재기동"
check "action" "restarted" "$(wd)"
for _ in $(seq 1 25); do [ -f "$WORK/state/supervisor.pid" ] && break; sleep 0.2; done
PID="$(cat "$WORK/state/supervisor.pid" 2>/dev/null || echo 0)"
kill -0 "$PID" 2>/dev/null && ALIVE=yes || ALIVE=no
check "래퍼 살아있음" "yes" "$ALIVE"

echo "[2] 이미 살아있음 → 무동작"
check "action" "ok" "$(wd)"

echo "[3] stop.sh 후 → 의도적 정지 존중"
bash "$WORK/scripts/stop.sh" >/dev/null
[ -f "$WORK/state/supervisor.stopped" ] && MARK=yes || MARK=no
check "정지 마커 생성" "yes" "$MARK"
check "action" "stopped" "$(wd)"
sleep 0.5
check "래퍼 안 살아남" "no" "$(kill -0 "$PID" 2>/dev/null && echo yes || echo no)"
check "재기동 안 함(pidfile 없음)" "no" "$([ -f "$WORK/state/supervisor.pid" ] && echo yes || echo no)"

echo "[4] start.sh → 마커 해제 후 감시 재개"
bash "$WORK/scripts/start.sh" >/dev/null
check "정지 마커 제거" "no" "$([ -f "$WORK/state/supervisor.stopped" ] && echo yes || echo no)"
check "action" "ok" "$(wd)"

echo
[ "$FAIL" -eq 0 ] && echo "test_watchdog.sh PASS" || echo "test_watchdog.sh FAIL"
exit "$FAIL"
