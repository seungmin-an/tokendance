#!/usr/bin/env python3
"""td — interactive control for tokendance workers. Thin front-end over the
file-state protocol + warm pool. stdlib-only."""
import argparse
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import status as S
import tasks as TK
import config
import pool
import checkpoint as CP


def _root(root=None):
    return root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hb_age(hb, now=None):
    if not hb:
        return None
    try:
        t = datetime.strptime(hb, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None
    return (now if now is not None else time.time()) - t


def _read(p):
    try:
        with open(p, errors="replace") as f:
            return f.read().strip()
    except OSError:
        return ""


def _tail(p, n):
    try:
        with open(p, errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return "(no log)"
    return "\n".join(lines[-n:])


def _pending_steer(task_dir):
    # read steer past the cursor WITHOUT advancing it (peek must not consume)
    try:
        with open(os.path.join(task_dir, "steer.cursor")) as f:
            cur = int(f.read().strip() or "0")
    except OSError:
        cur = 0
    try:
        with open(os.path.join(task_dir, "steer.md"), "rb") as f:
            f.seek(cur)
            return f.read().decode("utf-8", "replace").strip()
    except OSError:
        return ""


def cmd_status(root):
    rows = []
    for d in TK.list_tasks(root):
        age = _hb_age(d.get("heartbeat"))
        age_s = f"{int(age)}s" if age is not None else "-"
        rows.append((d["id"], d.get("state", ""), age_s,
                     str(d.get("attempts", 0)),
                     "paused" if d.get("paused") else ""))
    return rows


def cmd_peek(root, task_id, log_lines=20):
    root = _root(root)
    td_dir = S.task_dir(root, task_id)
    log = os.path.join(root, "state", "workers", f"{task_id}.log")
    return "\n".join([
        "== progress.md ==", _read(os.path.join(td_dir, "progress.md")) or "(empty)",
        "== pending steer ==", _pending_steer(td_dir) or "(none)",
        f"== worker log (last {log_lines}) ==", _tail(log, log_lines),
    ])


def cmd_logs(root, task_id, follow=False):
    log = os.path.join(root, "state", "workers", f"{task_id}.log")
    if follow:
        os.execvp("tail", ["tail", "-f", log])  # replaces process (thin follow)
    sys.stdout.write(_read(log) + "\n")


def cmd_pause(root, task_id):
    S.update(root, task_id, {"paused": True})


def cmd_resume(root, task_id):
    S.update(root, task_id, {"paused": False})


def cmd_steer(root, task_id, msg):
    td_dir = S.task_dir(root, task_id)
    with open(os.path.join(td_dir, "steer.md"), "ab") as f:
        f.write(f"\n[{_now()}] {msg}\n".encode("utf-8"))


def _worker_pid(root, task_id):
    pid = S.read(root, task_id).get("worker_pid")
    return int(pid) if pid else None


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _signal(pid, sig):
    try:
        os.kill(pid, sig)
    except OSError:
        pass


def _wait_dead(pid, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return not _alive(pid)


def _kill_worker(root, task_id, timeout=10.0):
    """SIGTERM the worker, wait for death, escalate to SIGKILL if needed.
    Returns True once the pid is confirmed dead; False if no/already-dead pid
    (or, in the worst case, still alive after SIGKILL+timeout)."""
    pid = _worker_pid(root, task_id)
    if pid is None or not _alive(pid):
        return False
    _signal(pid, signal.SIGTERM)
    if _wait_dead(pid, timeout * 0.5):
        return True
    _signal(pid, signal.SIGKILL)
    return _wait_dead(pid, timeout * 0.5)


def _script(root, name):
    return os.path.join(_root(root), "scripts", name)


def cmd_redirect(root, task_id, msg, runner=subprocess.run):
    cmd_steer(root, task_id, msg)
    _kill_worker(root, task_id)
    runner(["bash", _script(root, "launch-worker.sh"), task_id, "--resume"])


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:32] or "task"


def cmd_spawn(root, repo, desc, task_id=None):
    if task_id is None:
        task_id = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{_slug(desc)}"
    TK.create_task(root, task_id, title=desc, repo=os.path.abspath(repo))
    return task_id


def cmd_abort(root, task_id, mode="requeue", runner=subprocess.run):
    _kill_worker(root, task_id)
    runner(["bash", _script(root, "reclaim-worktree.sh"), task_id])
    if mode == "fail":
        S.update(root, task_id, {"state": "failed",
                                 "failure_reason": "aborted by operator",
                                 "worker_pid": None, "worker_session_id": None})
    else:
        S.update(root, task_id, {"state": "queued",
                                 "worker_pid": None, "worker_session_id": None})


def _repos(root):
    return sorted({os.path.abspath(d["repo"]) for d in TK.list_tasks(root) if d.get("repo")})


def cmd_disk(root, repo=None):
    repos = [os.path.abspath(repo)] if repo else _repos(root)
    result = []
    for r in repos:
        rep = pool.disk_report(r, root=root)
        result.append((r, rep["total_bytes"], rep["slots"]))
    return result


def cmd_worktree_ls(root, repo=None):
    repos = [os.path.abspath(repo)] if repo else _repos(root)
    rows = []
    for r in repos:
        rep = pool.disk_report(r, root=root)
        for e in rep["slots"]:
            rows.append({"repo": r, "name": e["name"],
                         "state": "leased" if e["leased"] else "idle",
                         "holder": e["holder"] or "",
                         "target_bytes": e["target_bytes"], "path": e["path"]})
    return rows


def cmd_gc(root, repo=None, dry_run=False):
    repos = [os.path.abspath(repo)] if repo else _repos(root)
    acts = []
    for r in repos:
        acts += pool.gc_targets(r, root=root, dry_run=dry_run)
    return acts


def _print_full_help(ap, sub):
    ap.print_help()
    for name in ("task", "worktree"):
        if name in sub.choices:
            print()
            sub.choices[name].print_help()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="td", description="tokendance interactive control")
    ap.add_argument("--root", default=None)
    sub = ap.add_subparsers(dest="cmd", required=False)
    task_p = sub.add_parser("task", help="inspect and control tasks (ls, peek, steer, spawn, …)")
    task_sub = task_p.add_subparsers(dest="task_cmd", required=True)
    task_sub.add_parser("ls", help="list all tasks (state, heartbeat age, attempts)")
    pk = task_sub.add_parser("peek", help="show a task's progress, pending steer, and recent log")
    pk.add_argument("task_id"); pk.add_argument("-n", type=int, default=20)
    lg = task_sub.add_parser("logs", help="print or follow (-f) a worker's log")
    lg.add_argument("task_id"); lg.add_argument("-f", action="store_true")
    sr = task_sub.add_parser("steer", help="append guidance a running worker reads at its next checkpoint")
    sr.add_argument("task_id"); sr.add_argument("msg")
    pa = task_sub.add_parser("pause", help="pause dispatch of a task"); pa.add_argument("task_id")
    re_ = task_sub.add_parser("resume", help="resume a paused task"); re_.add_argument("task_id")
    rd = task_sub.add_parser("redirect", help="steer + kill + relaunch a worker with --resume")
    rd.add_argument("task_id"); rd.add_argument("msg")
    ab = task_sub.add_parser("abort", help="kill a worker; requeue (or --fail)")
    ab.add_argument("task_id")
    ab.add_argument("--fail", action="store_true", help="mark failed instead of requeue")
    sp = task_sub.add_parser("spawn", help="create a queued coding task for a repo")
    sp.add_argument("--repo", required=True)
    sp.add_argument("desc"); sp.add_argument("--id", default=None)
    wt = sub.add_parser("worktree", help="inspect/manage the warm worktree pool (ls, disk, gc)")
    wt_sub = wt.add_subparsers(dest="wt_cmd", required=True)
    wt_ls = wt_sub.add_parser("ls", help="list pool slots with holder task, state, and target size")
    wt_ls.add_argument("--repo", default=None)
    wt_disk = wt_sub.add_parser("disk", help="show per-repo pool target/ disk usage")
    wt_disk.add_argument("--repo", default=None)
    wt_gc = wt_sub.add_parser("gc", help="reclaim idle/oversized pool target/ dirs (--dry-run to preview)")
    wt_gc.add_argument("--repo", default=None)
    wt_gc.add_argument("--dry-run", action="store_true")
    help_p = sub.add_parser("help", help="show help, optionally for a command")
    help_p.add_argument("topic", nargs="?")
    args = ap.parse_args(argv)
    if args.cmd is None or args.cmd == "help":
        topic = getattr(args, "topic", None)
        if topic and topic in sub.choices:
            sub.choices[topic].print_help()
        else:
            _print_full_help(ap, sub)
        return
    root = _root(args.root)
    if args.cmd == "task":
        if args.task_cmd == "ls":
            print(f"{'ID':24} {'STATE':12} {'HB':8} {'ATT':4} FLAG")
            for tid, st, age, att, flag in cmd_status(root):
                print(f"{tid:24} {st:12} {age:8} {att:4} {flag}")
        elif args.task_cmd == "peek":
            print(cmd_peek(root, args.task_id, log_lines=args.n))
        elif args.task_cmd == "logs":
            cmd_logs(root, args.task_id, follow=args.f)
        elif args.task_cmd == "steer":
            cmd_steer(root, args.task_id, args.msg)
        elif args.task_cmd == "pause":
            cmd_pause(root, args.task_id)
            if S.read(root, args.task_id).get("state") == "running":
                print(f"note: {args.task_id} is running; pause takes effect only when it requeues", file=sys.stderr)
        elif args.task_cmd == "resume":
            cmd_resume(root, args.task_id)
        elif args.task_cmd == "redirect":
            cmd_redirect(root, args.task_id, args.msg)
        elif args.task_cmd == "abort":
            cmd_abort(root, args.task_id, mode="fail" if args.fail else "requeue")
        elif args.task_cmd == "spawn":
            try:
                print(cmd_spawn(root, args.repo, args.desc, task_id=args.id))
            except ValueError as e:
                print(f"td task spawn: {e}", file=sys.stderr); raise SystemExit(1)
    elif args.cmd == "worktree":
        if args.wt_cmd == "ls":
            print(f"{'REPO':20} {'SLOT':5} {'STATE':7} {'HOLDER':25} {'TARGET':>8} PATH")
            for row in cmd_worktree_ls(root, repo=args.repo):
                print(f"{os.path.basename(row['repo']):20} {row['name']:5} {row['state']:7} "
                      f"{row['holder'] or '-':25} {row['target_bytes'] // (1024*1024):>6}M "
                      f"{row['path']}")
        elif args.wt_cmd == "disk":
            for r, total, _slots in cmd_disk(root, repo=args.repo):
                print(f"{os.path.basename(r):24} {total // (1024*1024):>8} MiB  {r}")
        elif args.wt_cmd == "gc":
            acts = cmd_gc(root, repo=args.repo, dry_run=args.dry_run)
            freed = sum(a.get("freed_bytes", 0) for a in acts)
            for a in acts:
                print(f"{a['name']}\t{a['reason']}\t{a.get('freed_bytes', 0)}")
            print(f"FREED\t{freed}")


if __name__ == "__main__":
    main()
