#!/usr/bin/env python3
"""supervisor 가 죽어 있으면 다시 띄우는 최소 워치독.

supervise.sh 는 supervisor.py 의 **크래시**만 복구한다. 래퍼 자체가 사라진 경우
(예: stop.sh 를 돌리고 재기동이 누락된 채 세션이 끝남)는 아무도 복구하지 않아
Slack 폴링이 조용히 멈춘다(2026-08-01 3시간 무응답 사고). 이 루프가 그 구멍을 막는다.

판정 기준은 **래퍼 프로세스 생존**뿐이고 tick 신선도는 쓰지 않는다 — 사서 패스가
ticks 를 13~15분, run_master(타임아웃 없는 subprocess.run)가 그 이상 막는 게 정상이라
staleness 로는 "죽음"과 "일하는 중"을 구분할 수 없다(실측 정상 공백 최대 50분).

의도적 정지 존중: stop.sh 가 남기는 state/supervisor.stopped 마커가 있으면 손대지 않는다.
start.sh 가 그 마커를 지운다.

  python3 scripts/watchdog.py --once     # 1회 점검(결과 출력)
  python3 scripts/watchdog.py            # 상주 루프(기본 60초)

기동(자기 프로세스그룹으로 분리 — stop.sh 의 그룹 kill 에 같이 죽지 않아야 한다):
  setsid nohup python3 /root/tokendance/scripts/watchdog.py >/dev/null 2>&1 &
중지: kill $(cat state/watchdog.pid)

주: start.sh 가 TOKENDANCE_CLAUDE 를 요구하므로 이 프로세스 env 에도 있어야 한다.
없으면 재기동이 실패하고 그 사실이 Slack 으로 통지된다(조용한 실패 없음).
"""
import argparse
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import slack as SLACK

DEFAULT_INTERVAL = 60


def _paths(root):
    st = os.path.join(root, "state")
    return {"pid": os.path.join(st, "supervisor.pid"),
            "stopped": os.path.join(st, "supervisor.stopped"),
            "own_pid": os.path.join(st, "watchdog.pid"),
            "log": os.path.join(st, "watchdog.log")}


def _log(root, msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(_paths(root)["log"], "a") as f:
        f.write(f"{ts} {msg}\n")


def supervisor_down(root):
    """래퍼 pid 가 없거나 그 프로세스가 살아있지 않으면 True."""
    try:
        with open(_paths(root)["pid"]) as f:
            pid = int(f.read().strip())
    except (FileNotFoundError, ValueError):
        return True
    try:
        os.kill(pid, 0)
    except OSError:
        return True
    return False


def _run_start(root):
    """scripts/start.sh 실행. 성공 여부 반환(래퍼가 이미 살아있어도 성공=0)."""
    p = subprocess.run(["bash", os.path.join(root, "scripts", "start.sh")],
                       cwd=root, capture_output=True, text=True)
    if p.returncode != 0:
        _log(root, f"start.sh 실패(rc={p.returncode}): {p.stderr.strip()[:300]}")
    return p.returncode == 0


def check_once(root, start=None):
    """한 번 점검하고 취한 조치를 반환: ok | stopped | restarted | restart-failed."""
    if os.path.exists(_paths(root)["stopped"]):
        return "stopped"                       # 의도적 정지 — 되살리지 않는다
    if not supervisor_down(root):
        return "ok"
    ok = (start or (lambda: _run_start(root)))()
    action = "restarted" if ok else "restart-failed"
    _log(root, f"supervisor 죽어있음 → {action}")
    return action


def should_notify(prev, cur):
    """알림은 엣지 트리거: 비정상 조치가 **새로** 발생했을 때만."""
    return cur in ("restarted", "restart-failed") and cur != prev


def _notify(root, action):
    text = (":wrench: supervisor 가 죽어 있어 워치독이 재기동했습니다."
            if action == "restarted"
            else ":rotating_light: supervisor 가 죽었는데 워치독의 재기동이 실패했습니다 "
                 "(state/watchdog.log 확인).")
    SLACK.post(root, text)


def _default_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=_default_root())
    ap.add_argument("--once", action="store_true", help="1회 점검 후 종료")
    ap.add_argument("--interval", type=int, default=DEFAULT_INTERVAL)
    args = ap.parse_args(argv)

    if args.once:
        print(check_once(args.root))
        return

    with open(_paths(args.root)["own_pid"], "w") as f:
        f.write(str(os.getpid()))
    _log(args.root, f"워치독 시작 pid={os.getpid()} interval={args.interval}s")
    prev = "ok"
    while True:
        try:
            action = check_once(args.root)
            if should_notify(prev, action):
                _notify(args.root, action)
            prev = action
        except Exception as e:                 # 루프는 절대 죽지 않는다
            _log(args.root, f"점검 오류: {e}")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
