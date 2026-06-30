#!/usr/bin/env python3
"""마스터 사이클의 기계적 부분을 수행하고, 판단이 필요한 일거리만 구조화해 알려준다.

  python3 scripts/cycle.py            # 기계 단계 수행(디스패치+harvest) 후 JSON plan 출력

기계 단계(코드가 직접):
  - queued 일감을 MAX_WORKERS 까지 launch-worker.sh 로 디스패치
  - harvest_knowledge.py 로 지식 승격
plan(JSON, 마스터가 판단할 것):
  - inbox_pending: 분류·처리할 사람 입력 [{name, text}]
  - review:        결과물 검수 대기 task id 들
  - needs_human / blocked: 사람에게 보고할 것
  - running / queued / counts: 참고

Slack pull/push 와 inbox 분류·리뷰는 LLM 판단이라 마스터(md)가 한다.
"""
import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tasks as TK
import config as C
import inbox as IB
import status as S


def dispatch_queued(root, launcher, max_workers):
    """빈 슬롯만큼 queued 를 디스패치. 디스패치한 task id 목록 반환."""
    dispatched = []
    queued = [d["id"] for d in TK.list_tasks(root, state="queued")]
    free = max_workers - TK.count_running(root)
    for tid in queued:
        if free <= 0:
            break
        if S.read(root, tid).get("paused"):
            continue
        if launcher(root, tid):
            dispatched.append(tid)
            free -= 1
    return dispatched


def _launch_argv(root, task_id):
    """디스패치는 항상 --resume 으로 기동: 반려 재큐(세션 있음)는 직전 컨텍스트를 이어받고,
    신규 queued(세션 없음)는 launch-worker.sh 가 자동으로 fresh 폴백한다(재투입 정합 #3)."""
    script = os.path.join(root, "scripts", "launch-worker.sh")
    return ["bash", script, task_id, "--resume"]


def _launch(root, task_id):
    try:
        r = subprocess.run(_launch_argv(root, task_id), cwd=root,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return r.returncode == 0
    except Exception:
        return False


def _run_harvest(root):
    script = os.path.join(root, "scripts", "harvest_knowledge.py")
    if not os.path.exists(script):
        return
    try:
        subprocess.run([sys.executable, script], cwd=root,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def build_plan(root):
    """판단이 필요한 일거리 + 참고 상태(JSON 직렬화 가능 dict). 부작용 없음."""
    def ids(state):
        return [d["id"] for d in TK.list_tasks(root, state=state)]

    pending = []
    for name in IB.list_pending(root):
        try:
            pending.append({"name": name, "text": IB.read_pending(root, name)})
        except OSError:
            pending.append({"name": name, "text": ""})

    all_tasks = TK.list_tasks(root)
    counts = {}
    for d in all_tasks:
        counts[d.get("state")] = counts.get(d.get("state"), 0) + 1

    return {
        "inbox_pending": pending,
        "review": ids("review"),
        "needs_human": ids("needs_human"),
        "blocked": ids("blocked"),
        "running": ids("running"),
        "queued": ids("queued"),
        "counts": counts,
    }


def _default_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=_default_root())
    ap.add_argument("--no-act", action="store_true",
                    help="기계 단계(디스패치/harvest) 생략하고 plan 만 출력")
    args = ap.parse_args(argv)
    root = args.root
    dispatched = []
    if not args.no_act:
        dispatched = dispatch_queued(root, _launch, C.get_int("MAX_WORKERS", r=root))
        _run_harvest(root)
    plan = build_plan(root)
    plan["dispatched_now"] = dispatched
    print(json.dumps(plan, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
