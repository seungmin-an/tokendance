#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
: "${TOKENDANCE_CLAUDE:?TOKENDANCE_CLAUDE 미설정}"
PIDFILE="$ROOT/state/supervisor.pid"
mkdir -p "$ROOT/state"
rm -f "$ROOT/state/supervisor.stopped"   # 의도적 정지 해제 → watchdog.py 감시 재개
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "이미 실행 중: $(cat "$PIDFILE")"; exit 0
fi
# keepalive 래퍼(supervise.sh)를 새 세션/프로세스그룹으로 기동 → supervisor.py 가 죽으면 자동 재기동.
# PIDFILE 에는 래퍼 pid(=프로세스그룹 리더)를 적는다. stop.sh 가 그룹째 종료한다.
# (POLL_INTERVAL 해석/로그 리다이렉트는 supervise.sh 안에서 처리.)
setsid nohup bash "$ROOT/scripts/supervise.sh" >/dev/null 2>&1 < /dev/null &
echo $! > "$PIDFILE"
echo "supervisor 시작(keepalive): $(cat "$PIDFILE")"
