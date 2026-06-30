#!/usr/bin/env python3
"""마스터 아침 루틴: 완료 worktree GC + 일일 다이제스트(Slack).

마스터 일감관리와 분리된 경량 루틴. supervisor 가 하루 1회(KST MASTER_MORNING_HOUR_KST,
기본 7시) 트리거한다. state/morning.last(KST 날짜)로 하루 중복을 막는다. librarian 과 달리
**LLM 불필요 — 순수 파이썬**(판단이 거의 없음): GC 는 git/파일 조작, 다이제스트는 status 집계.

두 가지를 한다:
  1. 완료 worktree GC — done(또는 archived)이고 결과가 보존된(브랜치가 base 에 머지됐거나
     remote 에 push 된) task 의 worktree + 로컬 task 브랜치를 제거. 안전 최우선:
       - 비종료 상태(running/needs_human/review/queued/blocked) task 는 절대 안 건드림.
       - 현재 실행 중인 워커 자신(current_task_id)도 제외.
       - 결과 미보존이면 삭제하지 않고 다이제스트에 "정리 후보(수동확인)"로 표기.
       - worktree 회수는 scripts/reclaim-worktree.sh 를 통해 풀 슬롯을 반환한다. 멱등.
  2. 일일 다이제스트 — 들고있는 작업(running/queued/review) + 사람 확인 필요(needs_human,
     대기사유 한 줄) + 정리 결과 + 최근 완료를 Slack 한 메시지로 보고.

게이트(should_run)/last-run 은 librarian 과 같은 모양이되 idle 을 요구하지 않는다 — 다이제스트는
진행 중 작업을 보고하는 게 목적이므로 매일 아침 무조건 돈다.

  python3 scripts/morning.py run            # GC + 다이제스트 + Slack 전송
  python3 scripts/morning.py run --print    # 전송하지 않고 다이제스트 텍스트만 출력
"""
import argparse
import os
import subprocess
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tasks as TK
import slack as SL
import status as S

KST = timezone(timedelta(hours=9))

# GC 적격 종료 상태. archived 는 현재 status STATES 에 없지만 forward-compat 로 포함(무해).
# failed 는 의도적으로 제외 — 조사/복구 여지가 있고 결과 보존이 불확실하므로 보호한다.
GC_ELIGIBLE_STATES = {"done", "archived"}


# ── 트리거 게이트 (KST 시각 + 하루 1회; librarian 과 달리 idle 불요) ──────────

def kst_date(now_utc):
    return now_utc.astimezone(KST).strftime("%Y-%m-%d")


def kst_hour(now_utc):
    return now_utc.astimezone(KST).hour


def should_run(now_utc, last_run_date, target_hour):
    """아침 루틴을 지금 돌려야 하는가(순수 함수).

    - KST 시각이 target_hour 일 때만.
    - 오늘(KST 날짜) 아직 안 돌았을 때만(하루 1회).
    idle 은 따지지 않는다(다이제스트는 진행 중 작업 보고가 목적).
    """
    if kst_hour(now_utc) != target_hour:
        return False
    return kst_date(now_utc) != (last_run_date or "")


def _last_path(root):
    return os.path.join(root, "state", "morning.last")


def read_last_run(root):
    try:
        with open(_last_path(root)) as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


def write_last_run(root, date_str):
    p = _last_path(root)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write(date_str)


def mark_run(root, now_utc):
    """오늘(KST) 돌았다고 기록 — 실행 결정 시점에 호출해 하루 중복을 막는다."""
    write_last_run(root, kst_date(now_utc))


# ── git 보조 (injectable runner) ─────────────────────────────────────────────

def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def _ok(runner, cmd):
    try:
        return runner(cmd).returncode == 0
    except Exception:
        return False


def branch_exists(repo, branch, runner=_run):
    if not branch:
        return False
    return _ok(runner, ["git", "-C", repo, "rev-parse", "--verify", "--quiet",
                        "refs/heads/" + branch])


def branch_preserved(repo, branch, runner=_run, bases=("main", "master")):
    """브랜치의 작업이 어딘가 durable 하게 보존됐는가.

    보존 = (a) 로컬 base(main/master)에 머지됨(merge-base --is-ancestor) OR
           (b) remote 에 push 됨(branch -r --contains 비어있지 않음).
    로컬에 브랜치가 없으면(이미 정리됨) 잃을 것이 없으므로 보존으로 본다.
    """
    if not branch_exists(repo, branch, runner):
        return True
    for base in bases:
        if base != branch and branch_exists(repo, base, runner):
            if _ok(runner, ["git", "-C", repo, "merge-base", "--is-ancestor", branch, base]):
                return True
    try:
        r = runner(["git", "-C", repo, "branch", "-r", "--contains", branch])
        if r.returncode == 0 and (r.stdout or "").strip():
            return True
    except Exception:
        pass
    return False


# ── GC 결정 (순수) ───────────────────────────────────────────────────────────

def gc_precheck(task, *, current_task_id=None,
                eligible_states=GC_ELIGIBLE_STATES):
    """facts 없이 가능한 싼 가드. ("eligible", None) 또는 ("skip", reason).

    git 을 부르기 전에 명백히 보호 대상인 task 를 걸러낸다.
    """
    tid = task["id"]
    state = task.get("state")
    if current_task_id is not None and tid == current_task_id:
        return ("skip", "현재 실행 중인 워커 자신")
    if state not in eligible_states:
        return ("skip", f"비종료 상태({state})")
    return ("eligible", None)


def gc_decision(task, facts, *, current_task_id=None,
                eligible_states=GC_ELIGIBLE_STATES):
    """worktree GC 결정(순수). 반환 {"action", "remove_worktree", "remove_branch", "reason"}.

    action ∈ {"remove", "candidate", "skip"}.
    facts = {"worktree_exists", "branch_exists", "branch_preserved"}.

    worktree 회수와 branch 삭제를 분리한다(사용자 directive 2026-06-26):
      - **worktree 회수: 공격적.** 백업이 있는 한(로컬 branch 존재 OR 결과 보존) 회수.
      - **branch 삭제: 보수적.** 결과가 보존(머지/푸시)됐을 때만 삭제. 미보존이면 남긴다(백업).
    로컬 branch 가 남아 있으면 그게 백업이므로, 머지/푸시 안 됐어도 worktree 는 회수해도 안전하다.
    """
    status, reason = gc_precheck(task, current_task_id=current_task_id,
                                 eligible_states=eligible_states)
    if status == "skip":
        return {"action": "skip", "remove_worktree": False, "remove_branch": False,
                "reason": reason}
    wt = facts["worktree_exists"]
    br = facts["branch_exists"]
    preserved = facts["branch_preserved"]
    # worktree 는 백업(branch 존재 OR 보존)이 있을 때만 회수. branch 는 보존됐을 때만 삭제.
    remove_wt = wt and (preserved or br)
    delete_br = br and preserved
    if remove_wt or delete_br:
        if delete_br and remove_wt:
            r = "done + 결과 보존 — worktree + branch 회수"
        elif remove_wt:
            r = "done + 로컬 branch 백업 존재 — worktree 회수(branch 보존)"
        else:
            r = "worktree 이미 회수 + 결과 보존 — branch 정리"
        return {"action": "remove", "remove_worktree": remove_wt,
                "remove_branch": delete_br, "reason": r}
    if not wt and not br:
        return {"action": "skip", "remove_worktree": False, "remove_branch": False,
                "reason": "이미 정리됨(멱등)"}
    if not wt:
        # worktree 는 이미 회수됨 + branch 는 백업으로 남김(미보존) → 할 일 없음.
        return {"action": "skip", "remove_worktree": False, "remove_branch": False,
                "reason": "worktree 이미 회수 · branch 백업 보존"}
    # worktree 존재하나 백업이 어디에도 없음(미보존 + branch 없음) — 이상 케이스 → 수동 확인.
    return {"action": "candidate", "remove_worktree": False, "remove_branch": False,
            "reason": "결과 미보존 + 로컬 branch 없음(백업 없음) — 수동 확인"}


# ── 사실 수집 + 실행 (IO) ────────────────────────────────────────────────────

def slot_path(root, task_id):
    """현재 task 의 풀 슬롯 경로(<task_dir>/worktree.path 에서 읽음).

    done task 는 state/tasks-done/<id>/ 로 이동하므로 resolver(S.task_dir)로
    양쪽 base 를 조회한다. 파일이 없으면 빈 문자열(슬롯 미할당 / 이미 반환).
    """
    p = os.path.join(S.task_dir(root, task_id), "worktree.path")
    try:
        with open(p) as f:
            return f.read().strip()
    except OSError:
        return ""


def gather_facts(root, task, runner=_run):
    repo = task.get("repo") or root
    branch = task.get("branch") or ""
    wt = slot_path(root, task["id"])
    return {
        "worktree_exists": bool(wt) and os.path.isdir(wt),
        "branch_exists": branch_exists(repo, branch, runner),
        "branch_preserved": branch_preserved(repo, branch, runner) if branch else False,
    }


def _default_reclaim(root, tid):
    reclaim = os.path.join(root, "scripts", "reclaim-worktree.sh")
    subprocess.run([reclaim, tid], check=True)


def execute_gc(root, task, decision, runner=_run, log=lambda m: None, dry_run=False,
               reclaim_fn=None):
    """decision.action == 'remove' 일 때만 worktree/브랜치를 제거. 실행(/예정) 단계 목록 반환.

    worktree 제거는 decision['remove_worktree'], branch 삭제는 decision['remove_branch'] 로
    독립적으로 게이트된다(둘 다 없으면 하위호환으로 True 취급). 미보존 done 은 remove_branch=False
    로 worktree 만 회수하고 로컬 branch 는 백업으로 남긴다.

    worktree 회수: reclaim_fn(root, task_id) 를 호출해 풀 슬롯을 반환한다
    (기본값은 reclaim-worktree.sh 호출; 테스트에서 교체 가능).
    branch 삭제: git branch -D tokendance/<id> (변경 없음).
    멱등: branch 가 이미 없으면 해당 단계를 건너뛴다.
    dry_run=True 면 같은 단계 문자열을 만들되 mutating 명령은 실행하지 않는다(미리보기).
    """
    if decision.get("action") != "remove":
        return []
    remove_wt = decision.get("remove_worktree", True)
    delete_br = decision.get("remove_branch", True)
    tid = task["id"]
    repo = task.get("repo") or root
    branch = task.get("branch") or ""
    _reclaim = reclaim_fn if reclaim_fn is not None else _default_reclaim
    prefix = "[dry-run] " if dry_run else ""
    steps = []
    if remove_wt:
        if not dry_run:
            _reclaim(root, tid)
        steps.append(f"{prefix}worktree 슬롯 반환: {tid}")
        log(f"GC {tid}: {prefix}worktree 슬롯 반환 (reclaim-worktree.sh)")
    if delete_br and branch and branch_exists(repo, branch, runner):
        if not dry_run:
            runner(["git", "-C", repo, "branch", "-D", branch])
        steps.append(f"{prefix}브랜치 삭제: {branch} ({repo})")
        log(f"GC {tid}: {prefix}브랜치 {branch} 삭제 ({repo})")
    return steps


def run_gc(root, tasks, *, current_task_id=None, runner=_run, log=lambda m: None, dry_run=False,
           reclaim_fn=None):
    """모든 task 를 돌며 GC 결정 + (remove 면) 실행. 결과 레코드 목록 반환.

    싼 가드(precheck)로 비종료/자기자신을 먼저 걸러 git 호출을 아낀다.
    dry_run=True 면 결정만 하고 실제 제거는 하지 않는다(미리보기).
    """
    out = []
    for t in tasks:
        status, reason = gc_precheck(t, current_task_id=current_task_id)
        if status == "skip":
            out.append({"task": t["id"], "state": t.get("state"),
                        "action": "skip", "reason": reason, "steps": []})
            continue
        facts = gather_facts(root, t, runner)
        dec = gc_decision(t, facts, current_task_id=current_task_id)
        steps = execute_gc(root, t, dec, runner=runner, log=log, dry_run=dry_run,
                           reclaim_fn=reclaim_fn)
        out.append({"task": t["id"], "state": t.get("state"),
                    "action": dec["action"], "reason": dec["reason"],
                    "remove_branch": dec.get("remove_branch", False), "steps": steps})
    return out


# ── 다이제스트 포맷 (순수) ───────────────────────────────────────────────────

# 🟢 "진행중" 하나로 묶는 상태들(각 줄엔 상태별 세부 이모지로 구분). steer(2026-06-26) 지정 순서.
_IN_PROGRESS = [("running", "🟢"), ("queued", "⏳"), ("review", "🔎")]


def _kst_str(now_utc):
    return now_utc.astimezone(KST).strftime("%Y-%m-%d %H:%M KST")


def _title_suffix(d):
    title = d.get("title") or ""
    return f" — {title}" if title else ""


def build_digest(tasks, gc_actions, *, now_str,
                 note_fn=lambda d: d.get("failure_reason") or ""):
    """아침 다이제스트 텍스트(순수).

    작업을 3 분류로 묶어 보여준다(steer 2026-06-26):
      🟢 진행중        — running/queued/review (마스터가 지금 들고있는 것).
      🟡 사람 확인 필요  — needs_human (봇이 막혀 사람 입력을 기다리는 것).
      ✅ 완료 · 사용자 미확인 — done (봇 기준 완료지만 사용자가 아직 안 본 것; 차단 아님, 사후 확인용).
    그 뒤 housekeeping: GC 로 정리한 worktree + 정리 후보(수동확인).

    tasks: status dict 목록. gc_actions: run_gc 결과 레코드 목록.
    note_fn: needs_human task 한 줄 대기사유 추출기(기본 failure_reason).
    """
    by = {}
    for d in tasks:
        by.setdefault(d.get("state"), []).append(d)

    L = [f"🌅 tokendance 아침 정리 ({now_str})", ""]

    # 🟢 진행중 (running/queued/review)
    in_prog = [(emoji, d) for st, emoji in _IN_PROGRESS for d in by.get(st, [])]
    L.append(f"🟢 진행중 ({len(in_prog)})")
    if in_prog:
        for emoji, d in in_prog:
            L.append(f"  • {emoji} {d['id']}{_title_suffix(d)}")
    else:
        L.append("  • 없음")

    # 🟡 사람 확인 필요 (needs_human)
    nh = by.get("needs_human", [])
    L.append("")
    L.append(f"🟡 사람 확인 필요 ({len(nh)})")
    if nh:
        for d in nh:
            note = note_fn(d)
            line = f"  • {d['id']}{_title_suffix(d)}"
            if note:
                line += f" · {note[:80]}"
            L.append(line)
    else:
        L.append("  • 없음")

    # ✅ 완료 · 사용자 미확인 (done) — needs_human 과 별개(차단 아님).
    done = by.get("done", [])
    L.append("")
    L.append(f"✅ 완료 · 사용자 미확인 ({len(done)})")
    if done:
        for d in done:
            L.append(f"  • {d['id']}{_title_suffix(d)}")
    else:
        L.append("  • 없음")

    # 🧹 housekeeping: GC 결과
    removed = [a for a in gc_actions if a["action"] == "remove"]
    candidates = [a for a in gc_actions if a["action"] == "candidate"]
    L.append("")
    L.append(f"🧹 정리한 worktree ({len(removed)})")
    if removed:
        for a in removed:
            detail = "; ".join(a.get("steps") or []) or "정리됨"
            # worktree+branch 둘 다 회수(보존됨) vs worktree 만 회수(branch 백업 보존) 구분.
            tag = "worktree+branch 회수" if a.get("remove_branch") else "worktree 회수(branch 보존)"
            L.append(f"  • {a['task']} [{tag}] — {detail}")
    else:
        L.append("  • 없음")
    if candidates:
        L.append("")
        L.append(f"⚠️ 정리 후보(수동확인) ({len(candidates)})")
        for a in candidates:
            L.append(f"  • {a['task']} — {a['reason']}")

    return "\n".join(L) + "\n"


# ── 통합 실행 (supervisor 가 호출) ───────────────────────────────────────────

def _log(msg):
    print(f"[morning] {msg}", file=sys.stderr, flush=True)


def _note(root, d):
    """needs_human task 의 대기사유 한 줄(failure_reason 우선, 없으면 progress.md 첫 줄)."""
    note = d.get("failure_reason") or ""
    if not note:
        p = os.path.join(S.task_dir(root, d["id"]), "progress.md")
        try:
            for line in open(p):
                s = line.strip().lstrip("#").strip()
                if s:
                    note = s
                    break
        except OSError:
            pass
    return note


def run_morning(root, now=None, post=True, runner=_run, log=_log, dry_run=False):
    """GC + 다이제스트 + (옵션) Slack 전송. 결과 dict 반환. 부작용은 git/Slack 뿐.

    dry_run=True 면 제거를 실행하지 않고 결정/다이제스트만 만든다(미리보기; post 도 권장 X).
    """
    if now is None:
        now = datetime.now(timezone.utc)
    tasks = TK.list_tasks(root)
    gc_actions = run_gc(root, tasks, runner=runner, log=log, dry_run=dry_run)
    text = build_digest(tasks, gc_actions, now_str=_kst_str(now),
                        note_fn=lambda d: _note(root, d))
    n_removed = sum(1 for a in gc_actions if a["action"] == "remove")
    n_cand = sum(1 for a in gc_actions if a["action"] == "candidate")
    log(f"아침 루틴{' [dry-run]' if dry_run else ''}: GC 제거 {n_removed} · "
        f"정리후보 {n_cand} · task {len(tasks)}")
    if post:
        try:
            SL.post(root, text)
        except Exception as e:
            log(f"Slack 전송 실패: {e}")
    return {"gc": gc_actions, "digest": text}


def _default_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main(argv=None):
    ap = argparse.ArgumentParser(description="마스터 아침 루틴(GC + 다이제스트).")
    ap.add_argument("--root", default=_default_root())
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--print", dest="only_print", action="store_true",
                   help="Slack 전송하지 않고 다이제스트 텍스트만 출력")
    r.add_argument("--dry-run", action="store_true",
                   help="실제 제거 없이 결정/다이제스트만(미리보기). Slack 전송도 생략.")
    args = ap.parse_args(argv)
    if args.cmd == "run":
        post = not (args.only_print or args.dry_run)
        res = run_morning(args.root, post=post, dry_run=args.dry_run)
        sys.stdout.write(res["digest"])


if __name__ == "__main__":
    main()
