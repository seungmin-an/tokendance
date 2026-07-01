#!/usr/bin/env python3
"""task 디렉토리 스캐폴딩/조회/카운트/아카이브."""
import argparse
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import status as S

TERMINAL_STATES = ("done", "failed")

_SCAFFOLD = {
    "task.md": "# {title}\n\n## 출처\n\n## 완료 기준\n",
    "progress.md": "",
    "steer.md": "",
    "steer.cursor": "0",
    "knowledge.md": "",
    "review.md": "",
}


def create_task(root, task_id, title="", repo=""):
    S.init(root, task_id, title=title, repo=repo)
    td = os.path.join(root, "state", "tasks", task_id)
    for name, tmpl in _SCAFFOLD.items():
        p = os.path.join(td, name)
        if not os.path.exists(p):
            with open(p, "w") as f:
                f.write(tmpl.format(title=title))
    return td


def list_tasks(root, state=None):
    # active(tasks/) + done(tasks-done/) 양쪽을 합쳐 본다. done 은 전용 디렉토리로 분리돼도
    # 목록/카운트에는 그대로 보여야 한다(archive 는 작업집합 밖이라 제외).
    out = []
    for tid in S.all_task_ids(root):
        d = S.read(root, tid)
        if state is None or d.get("state") == state:
            out.append(d)
    return out


def count_running(root):
    return len(list_tasks(root, state="running"))


def _worktree_has_tracked_changes(wt):
    """worktree 에 '추적 파일' 미커밋 변경(M/A/D/R/U…)이 있으면 True.

    untracked(`??` — 보통 빌드 산출물·심볼릭링크)는 무시한다: 커밋된 작업은 브랜치에 보존되고
    추적 변경만이 worktree 제거로 잃을 수 있는 '진짜 unsaved' 다. git 레포가 아니면 False.
    """
    r = subprocess.run(["git", "-C", wt, "status", "--porcelain"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return False
    return any(line[:2] != "??" for line in r.stdout.splitlines() if line.strip())


def archive(root, task_id):
    """종료(done/failed) task 를 state/tasks-archive/ 로 이동. 활성 task 는 거부.

    worktree 가 남아있으면 제거한다(커밋은 브랜치에 남으므로 안전). 단 추적 파일에 미커밋
    변경이 있으면 거부한다 — 그것만이 worktree 제거로 잃을 수 있는 진짜 unsaved 작업이다.
    **브랜치는 건드리지 않는다**(미푸시 커밋 손실 방지; 머지된 브랜치 정리는 morning 담당).
    반환: 이동된 아카이브 경로.
    """
    d = S.read(root, task_id)              # 없으면 여기서 에러
    state = d.get("state")
    if state not in TERMINAL_STATES:
        raise ValueError(f"종료 상태(done/failed)만 archive 가능 — 현재 '{state}'. 활성 task 는 보호됨.")

    wt = os.path.join(root, "state", "worktrees", task_id)
    # 안전 가드: 정확히 state/worktrees/<id> 직계만 다룬다(경로 탈출 방지).
    wt_abs = os.path.abspath(wt)
    wroot = os.path.abspath(os.path.join(root, "state", "worktrees"))
    if os.path.isdir(wt) and os.path.dirname(wt_abs) == wroot and os.path.basename(wt_abs) == task_id:
        if _worktree_has_tracked_changes(wt):
            raise ValueError(
                "worktree 에 미커밋(추적) 변경이 있음 — 커밋/정리 후 archive 하세요. "
                "(커밋된 작업은 브랜치에 보존되니, 정리하면 worktree 만 안전히 제거됨)")
        repo = d.get("repo") or root
        subprocess.run(["git", "-C", repo, "worktree", "remove", "--force", wt],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["git", "-C", repo, "worktree", "prune"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if os.path.isdir(wt):              # 등록 worktree 가 아니었거나 잔여 → 디렉토리만 정리
            shutil.rmtree(wt)

    src = S.task_dir(root, task_id)        # done→tasks-done/, failed→tasks/ 양쪽 해석
    dst_base = os.path.join(root, "state", "tasks-archive")
    os.makedirs(dst_base, exist_ok=True)
    dst = os.path.join(dst_base, task_id)
    if os.path.exists(dst):
        raise ValueError(f"이미 아카이브에 존재: {dst}")
    shutil.move(src, dst)
    return dst


def migrate_done(root):
    """기존 state/tasks/ 에 남아있는 done dir 들을 state/tasks-done/ 로 이동(멱등).

    done 디렉토리 분리 도입 전에 완료된 task 들을 lazy 가 아니라 일괄로 옮긴다. 재실행해도
    무해(이동 후 tasks/ 에는 done 이 없으므로 빈 목록 반환). 이동된 id 목록 반환.
    배포 시 마스터가 1회 실행한다(워커는 라이브 state/ 를 직접 조작하지 않음).
    """
    moved = []
    base = os.path.join(root, "state", S.ACTIVE_BASE)
    if not os.path.isdir(base):
        return moved
    for tid in sorted(os.listdir(base)):
        if not os.path.exists(os.path.join(base, tid, "status.json")):
            continue
        if S.read(root, tid).get("state") == "done":
            S.relocate(root, tid)
            moved.append(tid)
    return moved


def _default_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=_default_root())
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("new")
    p.add_argument("task_id")
    p.add_argument("--title", default="")
    p.add_argument("--repo", default="")
    p = sub.add_parser("list")
    p.add_argument("--state")
    sub.add_parser("count-running")
    p = sub.add_parser("archive")
    p.add_argument("task_id")
    sub.add_parser("migrate-done")
    args = ap.parse_args(argv)
    if args.cmd == "new":
        print(create_task(args.root, args.task_id, args.title, args.repo))
    elif args.cmd == "list":
        for d in list_tasks(args.root, args.state):
            print(f"{d['id']}\t{d['state']}\t{d.get('title','')}")
    elif args.cmd == "count-running":
        print(count_running(args.root))
    elif args.cmd == "archive":
        print("archived →", archive(args.root, args.task_id))
    elif args.cmd == "migrate-done":
        moved = migrate_done(args.root)
        print(f"migrated {len(moved)} done → tasks-done/: {', '.join(moved) or '(none)'}")


if __name__ == "__main__":
    main()
