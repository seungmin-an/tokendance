#!/usr/bin/env python3
"""td — interactive control for tokendance workers. Thin front-end over the
file-state protocol + warm pool. stdlib-only."""
import argparse
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import status as S
import tasks as TK
import config
import pool
import checkpoint as CP
import backlog as BL


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


def _age(iso, now=None):
    """Compact human age (3d/5h/2m/10s) from an ISO-UTC timestamp; '-' if unparseable."""
    secs = _hb_age(iso, now)
    if secs is None:
        return "-"
    secs = int(secs)
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= size:
            return f"{secs // size}{unit}"
    return f"{secs}s"


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


def _worktree_path(root, task_id):
    """The task's leased worktree abs path (written by prepare-worktree.sh to
    <task_dir>/worktree.path). None if unrecorded. The path travels with the task
    dir on state moves, so resolving via S.task_dir stays correct for any state."""
    return _read(os.path.join(S.task_dir(root, task_id), "worktree.path")) or None


def _exec_attach(claude, argv, cwd, env):
    """Replace this process with an interactive claude in the worktree (like
    cmd_logs' tail-follow). Wrapped so tests can inject a spy instead of exec."""
    os.chdir(cwd)
    os.execvpe(claude, argv, env)


def cmd_attach(root, task_id, skip_permissions=False, claude_bin=None, execer=_exec_attach):
    """Hand a worker's session over to a human at an interactive terminal.

    Stops the worker first (pause → confirm dead) so one session file isn't driven
    by two processes at once, then execs `claude --resume <sid>` in the worktree.
    Leaves the task queued+paused with no pid: `queued` keeps the supervisor's
    stale detector (running-only) from relaunching a headless worker onto the live
    session, `paused` blocks dispatch during the handover, and the pair lets a later
    `td task resume` auto-continue via launch-worker.sh --resume (dispatch is
    queued-only). The session id is preserved so that resume picks up where the
    human left off."""
    root = _root(root)
    d = S.read(root, task_id)
    sid = d.get("worker_session_id")
    if not sid:
        print(f"td task attach: {task_id} has no worker_session_id — nothing to attach",
              file=sys.stderr)
        raise SystemExit(1)
    wt = _worktree_path(root, task_id)
    if not wt or not os.path.isdir(wt):
        print(f"td task attach: no worktree for {task_id} ({wt or 'unrecorded'}) — cannot attach",
              file=sys.stderr)
        raise SystemExit(1)
    claude = claude_bin or os.environ.get("TOKENDANCE_CLAUDE")
    if not claude:
        print("td task attach: TOKENDANCE_CLAUDE unset — cannot locate the claude binary",
              file=sys.stderr)
        raise SystemExit(1)
    # Safe takeover: block re-dispatch first, then stop the worker if it's still
    # alive (two processes on one session file corrupt/fork it). Dead worker → skip.
    S.update(root, task_id, {"paused": True})
    pid = _worker_pid(root, task_id)
    if pid is not None and _alive(pid):
        _kill_worker(root, task_id)
    # State reconciliation (#5): out of `running` (no stale-relaunch), pid cleared.
    S.update(root, task_id, {"state": "queued", "worker_pid": None})
    argv = [claude, "--resume", sid]
    if skip_permissions:
        argv.append("--dangerously-skip-permissions")
    env = {**os.environ, "IS_SANDBOX": "1"}   # allow claude to boot as root
    print(f"attaching to {task_id}: resuming session {sid[:8]} in {wt}\n"
          f"worker stopped + paused; run 'td task resume {task_id}' after you exit "
          f"to auto-continue headless.", file=sys.stderr)
    execer(claude, argv, wt, env)


def _recall_block(root, repo):
    """Library recall for a worker on `repo` — the same source launch-worker.sh
    injects (harvest_knowledge.recall_block, which `--recall` also calls).
    Best-effort: any failure yields "" so it never blocks provisioning."""
    try:
        import harvest_knowledge as HK
        return HK.recall_block(root, os.path.abspath(repo))
    except Exception:
        return ""


def cmd_open(root, repo, desc="", task_id=None, skip_permissions=False,
             claude_bin=None, runner=subprocess.run, which=shutil.which,
             recall_fn=_recall_block):
    """Provision a human-driven worktree session (tmux + claude + recall).

    The forward mirror of cmd_attach: attach hands an *existing* worker session to
    a human, open *starts* a fresh one a human drives — and both end in the same
    state so the two are reversible. A detached tmux session `td-<id>` is created
    whose window runs `claude --session-id <new-uuid>` in the leased worktree with
    the repo's library recall appended; the user runs `tmux attach -t td-<id>`.

    The task is left queued+paused with the minted session id and no pid: `queued`
    keeps the supervisor's stale detector (running-only) from launching a headless
    worker onto the live session, `paused` blocks dispatch while the human drives,
    and the pair lets a later `td task resume` offload the work headless via
    launch-worker.sh --resume (dispatch is queued-only), reusing the same session."""
    root = _root(root)
    claude = claude_bin or os.environ.get("TOKENDANCE_CLAUDE")
    if not claude:
        print("td task open: TOKENDANCE_CLAUDE unset — cannot locate the claude binary",
              file=sys.stderr)
        raise SystemExit(1)
    tmux = which("tmux")
    if not tmux:
        print("td task open: tmux not found on PATH — install tmux or run headless via 'td task spawn'",
              file=sys.stderr)
        raise SystemExit(1)
    # Create the tracked task (reuses spawn's id rule + status scaffolding), then
    # pause immediately so dispatch can't grab it before the worktree/session exist.
    task_id = cmd_spawn(root, repo, desc, task_id=task_id)
    S.update(root, task_id, {"paused": True})
    # Provision the isolated worktree (pool lease + branch tokendance/<id> + records
    # worktree.path). Pass root through so prepare-worktree resolves the same tree.
    prep = runner(["bash", _script(root, "prepare-worktree.sh"), task_id],
                  capture_output=True, text=True,
                  env={**os.environ, "TOKENDANCE_ROOT": root})
    if getattr(prep, "returncode", 1) != 0:
        print(f"td task open: worktree provisioning failed for {task_id} "
              f"(discard with 'td task abort {task_id}'):\n{getattr(prep, 'stderr', '') or ''}".rstrip(),
              file=sys.stderr)
        raise SystemExit(1)
    wt = _worktree_path(root, task_id)
    if not wt or not os.path.isdir(wt):
        print(f"td task open: no worktree recorded for {task_id} ({wt or 'unrecorded'}) "
              f"— discard with 'td task abort {task_id}'", file=sys.stderr)
        raise SystemExit(1)
    # Mint the session up front (like launch-worker) and record it; worker_pid stays
    # None (init default). resume tolerates a missing session file → clean fresh boot.
    sid = str(uuid.uuid4())
    S.update(root, task_id, {"worker_session_id": sid})
    recall = recall_fn(root, repo)
    inner = ["env", "IS_SANDBOX=1", claude, "--session-id", sid]
    if recall:                                    # empty recall → omit the flag entirely
        inner += ["--append-system-prompt", recall]
    if skip_permissions:                          # default keeps interactive permission prompts
        inner.append("--dangerously-skip-permissions")
    session_name = f"td-{task_id}"
    shell_cmd = " ".join(shlex.quote(a) for a in inner)   # tmux runs this via the shell
    tmux_cmd = [tmux, "new-session", "-d", "-s", session_name, "-c", wt, shell_cmd]
    res = runner(tmux_cmd, capture_output=True, text=True)
    if getattr(res, "returncode", 1) != 0:
        print(f"td task open: tmux failed to create session {session_name} "
              f"(name in use? kill it or discard with 'td task abort {task_id}'):\n"
              f"{getattr(res, 'stderr', '') or ''}".rstrip(), file=sys.stderr)
        raise SystemExit(1)
    print(f"opened {task_id}: human-driven session {session_name} (detached) in {wt}\n"
          f"  attach:  tmux attach -t {session_name}\n"
          f"  offload: td task resume {task_id}   (hand off to a headless worker)\n"
          f"  reclaim: td task abort {task_id}    (discard worktree when done)",
          file=sys.stderr)
    return task_id


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


def _subcommands(parser):
    """Return a parser's direct subcommands as [(name, help)] in definition
    order, by introspecting its SubParsersAction (single-sourced from the
    help= text passed to add_parser — no duplicated descriptions)."""
    for act in parser._actions:
        if isinstance(act, argparse._SubParsersAction):
            return [(a.dest, a.help or "") for a in act._get_subactions()]
    return []


def _ap_choice(ap, name):
    for act in ap._actions:
        if isinstance(act, argparse._SubParsersAction):
            return act.choices.get(name)
    return None


def _print_tree(ap):
    print("td — tokendance interactive control\n")
    groups = _subcommands(ap)
    for gi, (g, gdesc) in enumerate(groups):
        glast = gi == len(groups) - 1
        print(f"{'└─' if glast else '├─'} {g:9} {gdesc}")
        subs = _subcommands(_ap_choice(ap, g))
        cont = "   " if glast else "│  "
        for si, (s, sdesc) in enumerate(subs):
            print(f"{cont}{'└─' if si == len(subs) - 1 else '├─'} {s:9} {sdesc}")
    print("\nRun 'td <group> <command> -h' for command details.")


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
    at = task_sub.add_parser("attach", help="stop a worker and take over its session interactively (claude --resume)")
    at.add_argument("task_id")
    at.add_argument("--skip-permissions", action="store_true",
                    help="pass --dangerously-skip-permissions (default: keep interactive permission prompts)")
    sp = task_sub.add_parser("spawn", help="create a queued coding task for a repo")
    sp.add_argument("--repo", required=True)
    sp.add_argument("desc"); sp.add_argument("--id", default=None)
    op = task_sub.add_parser("open", help="provision a human-driven worktree session (tmux + claude + recall); offload later with resume")
    op.add_argument("--repo", required=True)
    op.add_argument("desc", nargs="?", default="")
    op.add_argument("--id", default=None)
    op.add_argument("--skip-permissions", action="store_true",
                    help="pass --dangerously-skip-permissions (default: keep interactive permission prompts)")
    wt = sub.add_parser("worktree", help="inspect/manage the warm worktree pool (ls, disk, gc)")
    wt_sub = wt.add_subparsers(dest="wt_cmd", required=True)
    wt_ls = wt_sub.add_parser("ls", help="list pool slots with holder task, state, and target size")
    wt_ls.add_argument("--repo", default=None)
    wt_disk = wt_sub.add_parser("disk", help="show per-repo pool target/ disk usage")
    wt_disk.add_argument("--repo", default=None)
    wt_gc = wt_sub.add_parser("gc", help="reclaim idle/oversized pool target/ dirs (--dry-run to preview)")
    wt_gc.add_argument("--repo", default=None)
    wt_gc.add_argument("--dry-run", action="store_true")
    bl = sub.add_parser("backlog", help="idea backlog: add, list, tag, promote to a task")
    bl_sub = bl.add_subparsers(dest="bl_cmd", required=True)
    bl_add = bl_sub.add_parser("add", help="add an idea to the backlog")
    bl_add.add_argument("text")
    bl_add.add_argument("--tag", action="append", default=[], dest="tags")
    bl_ls = bl_sub.add_parser("ls", help="list backlog entries (filter by --tag / --status)")
    bl_ls.add_argument("--tag", default=None)
    bl_ls.add_argument("--status", choices=BL.STATUSES, default=None)
    bl_show = bl_sub.add_parser("show", help="show a backlog entry in full")
    bl_show.add_argument("id")
    bl_tag = bl_sub.add_parser("tag", help="add tags to an entry (or --remove them)")
    bl_tag.add_argument("id"); bl_tag.add_argument("tags", nargs="+")
    bl_tag.add_argument("--remove", action="store_true")
    bl_pr = bl_sub.add_parser("promote", help="promote an entry to a queued task for a repo")
    bl_pr.add_argument("id"); bl_pr.add_argument("--repo", required=True)
    bl_pr.add_argument("--id", dest="task_id", default=None)
    bl_drop = bl_sub.add_parser("drop", help="mark an entry dropped")
    bl_drop.add_argument("id")
    help_p = sub.add_parser("help", help="show help, optionally for a command")
    help_p.add_argument("topic", nargs="?")
    args = ap.parse_args(argv)
    if args.cmd is None or args.cmd == "help":
        topic = getattr(args, "topic", None)
        if topic and topic in sub.choices:
            sub.choices[topic].print_help()
        else:
            _print_tree(ap)
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
        elif args.task_cmd == "attach":
            cmd_attach(root, args.task_id, skip_permissions=args.skip_permissions)
        elif args.task_cmd == "spawn":
            try:
                print(cmd_spawn(root, args.repo, args.desc, task_id=args.id))
            except ValueError as e:
                print(f"td task spawn: {e}", file=sys.stderr); raise SystemExit(1)
        elif args.task_cmd == "open":
            try:
                print(cmd_open(root, args.repo, args.desc, task_id=args.id,
                               skip_permissions=args.skip_permissions))
            except ValueError as e:
                print(f"td task open: {e}", file=sys.stderr); raise SystemExit(1)
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
    elif args.cmd == "backlog":
        if args.bl_cmd == "add":
            print(BL.add(root, args.text, args.tags))
        elif args.bl_cmd == "ls":
            print(f"{'ID':40} {'STATUS':9} {'AGE':5} {'TAGS':16} TEXT")
            for e in BL.ls(root, tag=args.tag, status=args.status):
                first = (e.get("text") or "").strip().splitlines()
                print(f"{e['id']:40} {e['status']:9} {_age(e.get('created')):5} "
                      f"{','.join(e.get('tags', [])):16} {(first[0] if first else '')[:60]}")
        elif args.bl_cmd == "show":
            try:
                e = BL.get(root, args.id)
            except ValueError as err:
                print(f"td backlog show: {err}", file=sys.stderr); raise SystemExit(1)
            print(f"id:        {e['id']}")
            print(f"created:   {e.get('created', '')}")
            print(f"status:    {e.get('status', '')}")
            print(f"tags:      {', '.join(e.get('tags', []))}")
            print(f"promoted:  {e.get('promoted_task_id') or '-'}")
            print(f"\n{e.get('text', '')}")
        elif args.bl_cmd == "tag":
            try:
                BL.tag(root, args.id, args.tags, remove=args.remove)
            except ValueError as err:
                print(f"td backlog tag: {err}", file=sys.stderr); raise SystemExit(1)
        elif args.bl_cmd == "promote":
            try:
                print(BL.promote(root, args.id, args.repo, task_id=args.task_id))
            except ValueError as err:
                print(f"td backlog promote: {err}", file=sys.stderr); raise SystemExit(1)
        elif args.bl_cmd == "drop":
            try:
                BL.drop(root, args.id)
            except ValueError as err:
                print(f"td backlog drop: {err}", file=sys.stderr); raise SystemExit(1)


if __name__ == "__main__":
    main()
