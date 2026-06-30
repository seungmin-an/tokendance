#!/usr/bin/env python3
"""status.json 의 유일한 변경 통로. flock 직렬화 + atomic rename + version 관리."""
import argparse
import fcntl
import json
import os
import sys
import tempfile
from datetime import datetime, timezone

STATES = {"queued", "running", "needs_human", "blocked", "review", "done", "failed"}

# task 디렉토리 base 들. done 은 전용 디렉토리로 분리해 active 와 섞이지 않게 한다.
# (archive 는 별개 — 수동 tasks.py archive 로만 이동하며 작업집합에서 제외되므로 여기 없음.)
ACTIVE_BASE = "tasks"        # queued/running/needs_human/blocked/review/failed
DONE_BASE = "tasks-done"     # done 전용(사용자가 done 만 모아 보고 PR 화)
TASK_BASES = (ACTIVE_BASE, DONE_BASE)


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _dir_in(root, base, task_id):
    return os.path.join(root, "state", base, task_id)


def task_dir(root, task_id):
    """임의 id 의 task 디렉토리를 양쪽 base 에서 해석(resolver).

    done 은 tasks-done/ 에 산다 → 그쪽을 우선 조회(done=권위). 둘 다 없으면 tasks/
    경로를 반환한다(신규 생성 기본 + 마이그레이션 전 하위호환).
    """
    for base in (DONE_BASE, ACTIVE_BASE):
        if os.path.exists(os.path.join(_dir_in(root, base, task_id), "status.json")):
            return _dir_in(root, base, task_id)
    return _dir_in(root, ACTIVE_BASE, task_id)


def all_task_ids(root):
    """양쪽 base(tasks/ + tasks-done/)의 status.json 보유 id 들(정렬·유니크)."""
    tids = set()
    for base in TASK_BASES:
        bdir = os.path.join(root, "state", base)
        if not os.path.isdir(bdir):
            continue
        for tid in os.listdir(bdir):
            if os.path.exists(os.path.join(bdir, tid, "status.json")):
                tids.add(tid)
    return sorted(tids)


def _canonical_base(state):
    """state 가 살아야 할 base. done 만 tasks-done/, 나머지는 모두 tasks/."""
    return DONE_BASE if state == "done" else ACTIVE_BASE


def _reconcile_location(root, task_id, state):
    """task dir 를 state 의 canonical base 로 이동(멱등). 새 경로 반환.

    done 전환 → tasks-done/ 로, done→다른상태 되돌림 → tasks/ 로. 같은 fs 라 os.rename 은
    atomic. 호출자는 status.json.lock 을 쥔 채로 부른다 — flock 은 inode 기반이라 디렉토리를
    rename 해도 fd 가 유효하고, 다른 프로세스는 resolver 로 새 위치를 다시 찾아 같은 inode 에
    flock 하므로 상호배제가 유지된다. dir 이 이미 canonical 위치면 아무 것도 하지 않는다.
    """
    dst = _dir_in(root, _canonical_base(state), task_id)
    if os.path.isdir(dst):
        return dst
    for base in TASK_BASES:
        cur = _dir_in(root, base, task_id)
        if cur != dst and os.path.isdir(cur):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            os.rename(cur, dst)
            return dst
    return dst


def relocate(root, task_id):
    """현재 state 기준 canonical base 로 이동(멱등). 마이그레이션/복구용. 새 경로 반환."""
    if not os.path.exists(_status_path(root, task_id)):
        raise ValueError(f"no such task: {task_id}")
    with _Lock(_lock_path(root, task_id)):
        sp = _status_path(root, task_id)
        if not os.path.exists(sp):
            raise ValueError(f"no such task: {task_id}")
        with open(sp) as f:
            state = json.load(f).get("state")
        return _reconcile_location(root, task_id, state)


def _task_dir(root, task_id):       # 하위호환 alias (내부 사용처용)
    return task_dir(root, task_id)


def _status_path(root, task_id):
    return os.path.join(_task_dir(root, task_id), "status.json")


def _lock_path(root, task_id):
    return os.path.join(_task_dir(root, task_id), "status.json.lock")


class _Lock:
    def __init__(self, path):
        self.path = path
        self.fd = None

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.fd = open(self.path, "w")
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX)
        except BaseException:
            self.fd.close()
            raise
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        self.fd.close()


def _atomic_write(path, data):
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".status.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def read(root, task_id):
    with open(_status_path(root, task_id)) as f:
        return json.load(f)


def init(root, task_id, title="", repo=""):
    with _Lock(_lock_path(root, task_id)):
        sp = _status_path(root, task_id)
        if os.path.exists(sp):
            raise ValueError(f"task already exists: {task_id}")
        data = {
            "id": task_id, "title": title, "repo": repo,
            "state": "queued", "version": 1,
            "worker_pid": None, "worker_session_id": None, "branch": None,
            "heartbeat": None, "launched_at": None,
            "created": _now(), "updated": _now(),
            "attempts": 0, "failure_reason": None,
        }
        _atomic_write(sp, data)
        return data


def update(root, task_id, changes, expected_version=None, increment_attempts=False):
    # 멱등/유령 방지: 없는 task(예: 이미 archive 됨)면 락 디렉토리(=task 디렉토리)를
    # 만들기 전에 차단한다. 안 그러면 _Lock 의 makedirs 가 빈 task 디렉토리를 되살린다.
    if not os.path.exists(_status_path(root, task_id)):
        raise ValueError(f"no such task: {task_id}")
    with _Lock(_lock_path(root, task_id)):
        sp = _status_path(root, task_id)
        if not os.path.exists(sp):       # 락 안에서 재확인(경합 방어)
            raise ValueError(f"no such task: {task_id}")
        with open(sp) as f:
            data = json.load(f)
        if expected_version is not None and data.get("version") != expected_version:
            raise ValueError(
                f"version mismatch: expected {expected_version}, got {data.get('version')}")
        data.update(changes)
        if increment_attempts:
            data["attempts"] = data.get("attempts", 0) + 1
        if "state" in data and data["state"] not in STATES:
            raise ValueError(f"invalid state: {data['state']}")
        if data.get("state") == "failed" and not data.get("failure_reason"):
            raise ValueError("state=failed requires failure_reason")
        data["version"] = data.get("version", 0) + 1
        data["updated"] = _now()
        _atomic_write(sp, data)
        # 락을 쥔 채 canonical base 로 정렬(done→tasks-done/, 그 외→tasks/). 멱등.
        _reconcile_location(root, task_id, data.get("state"))
        return data


def heartbeat(root, task_id):
    return update(root, task_id, {"heartbeat": _now()})


def _default_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main(argv=None):
    ap = argparse.ArgumentParser(description="status.json 변경 통로")
    ap.add_argument("--root", default=_default_root())
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init")
    p.add_argument("task_id")
    p.add_argument("--title", default="")
    p.add_argument("--repo", default="")

    p = sub.add_parser("set")
    p.add_argument("task_id")
    p.add_argument("--state")
    p.add_argument("--pid", type=int)
    p.add_argument("--session")
    p.add_argument("--branch")
    p.add_argument("--failure-reason")
    p.add_argument("--launched-now", action="store_true",
                   help="launched_at 을 현재 시각으로 기록(워커 디스패치 시점)")
    p.add_argument("--bump-attempts", action="store_true")
    p.add_argument("--paused", dest="paused", action="store_true", default=None)
    p.add_argument("--no-paused", dest="paused", action="store_false")
    p.add_argument("--expected-version", type=int)

    p = sub.add_parser("get")
    p.add_argument("task_id")
    p.add_argument("--field")

    p = sub.add_parser("heartbeat")
    p.add_argument("task_id")

    args = ap.parse_args(argv)

    if args.cmd == "init":
        print(json.dumps(init(args.root, args.task_id, args.title, args.repo)))
    elif args.cmd == "set":
        changes = {}
        if args.state is not None:
            changes["state"] = args.state
        if args.pid is not None:
            changes["worker_pid"] = args.pid
        if args.session is not None:
            changes["worker_session_id"] = args.session
        if args.branch is not None:
            changes["branch"] = args.branch
        if args.failure_reason is not None:
            changes["failure_reason"] = args.failure_reason
        if args.launched_now:
            changes["launched_at"] = _now()
        if args.paused is not None:
            changes["paused"] = args.paused
        print(json.dumps(update(args.root, args.task_id, changes,
                                expected_version=args.expected_version,
                                increment_attempts=args.bump_attempts)))
    elif args.cmd == "get":
        d = read(args.root, args.task_id)
        if args.field:
            if args.field not in d:
                raise SystemExit(f"no such field: {args.field} (fields: {', '.join(d)})")
            print(d[args.field])
        else:
            print(json.dumps(d, ensure_ascii=False))
    elif args.cmd == "heartbeat":
        print(json.dumps(heartbeat(args.root, args.task_id)))


if __name__ == "__main__":
    main()
