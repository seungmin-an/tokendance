#!/usr/bin/env python3
"""td — interactive control for tokendance workers. Thin front-end over the
file-state protocol + warm pool. stdlib-only."""
import argparse
import os
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


def cmd_steer(root, task_id, msg):
    td_dir = S.task_dir(root, task_id)
    with open(os.path.join(td_dir, "steer.md"), "a") as f:
        f.write(f"\n[{_now()}] {msg}\n")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="td", description="tokendance interactive control")
    ap.add_argument("--root", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    pk = sub.add_parser("peek"); pk.add_argument("task_id"); pk.add_argument("-n", type=int, default=20)
    lg = sub.add_parser("logs"); lg.add_argument("task_id"); lg.add_argument("-f", action="store_true")
    sr = sub.add_parser("steer"); sr.add_argument("task_id"); sr.add_argument("msg")
    args = ap.parse_args(argv)
    root = _root(args.root)
    if args.cmd == "status":
        print(f"{'ID':24} {'STATE':12} {'HB':8} {'ATT':4} FLAG")
        for tid, st, age, att, flag in cmd_status(root):
            print(f"{tid:24} {st:12} {age:8} {att:4} {flag}")
    elif args.cmd == "peek":
        print(cmd_peek(root, args.task_id, log_lines=args.n))
    elif args.cmd == "logs":
        cmd_logs(root, args.task_id, follow=args.f)
    elif args.cmd == "steer":
        cmd_steer(root, args.task_id, args.msg)


if __name__ == "__main__":
    main()
