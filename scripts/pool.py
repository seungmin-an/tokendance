#!/usr/bin/env python3
"""Warm worktree pool: lease/reuse git worktrees so parallel workers keep warm
build caches (per-slot target/) without re-paying full builds. Ported from
treehouse's lease-pool model; stdlib-only. State is flock-serialized JSON."""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import subprocess
import time

try:
    from scripts import config
except ImportError:  # running with scripts/ on sys.path
    import config


def _root(root=None):
    return root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _repo_key(repo):
    name = os.path.basename(os.path.normpath(repo))
    h = hashlib.sha256(os.path.abspath(repo).encode()).hexdigest()[:6]
    return f"{name}-{h}"


def pool_dir(repo, root=None):
    return os.path.join(_root(root), "state", "pool", _repo_key(repo))


def _state_path(pdir):
    return os.path.join(pdir, "state.json")


@contextlib.contextmanager
def state_lock(pdir):
    os.makedirs(pdir, exist_ok=True)
    f = open(os.path.join(pdir, "state.json.lock"), "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(f, fcntl.LOCK_UN)
        f.close()


def load_state(pdir):
    try:
        with open(_state_path(pdir)) as f:
            return json.load(f)
    except FileNotFoundError:
        return {"entries": []}


def save_state(pdir, state):
    os.makedirs(pdir, exist_ok=True)
    tmp = _state_path(pdir) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, _state_path(pdir))


def git(repo, *args, check=True):
    return subprocess.run(["git", "-C", repo, *args], check=check,
                          capture_output=True, text=True)


def default_ref(repo):
    r = git(repo, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD", check=False)
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip()
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def is_dirty(wt):
    r = git(wt, "status", "--porcelain", "--untracked-files=all")
    return bool(r.stdout.strip())


def add_worktree(repo, path, ref):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    git(repo, "worktree", "add", "--detach", "--force", path, ref)


def reset_worktree(wt, ref):
    git(wt, "checkout", "--detach", "--force", ref)
    git(wt, "reset", "--hard", ref)
    git(wt, "clean", "-fd")  # NOTE: no -x — preserves gitignored target/ + .venv symlink


def shared_dirs(root=None):
    raw = config.get("POOL_SHARED_SYMLINKS", ".venv", r=root)
    return [s.strip() for s in raw.split(",") if s.strip()]


def apply_shared_symlinks(repo, wt, root=None):
    for rel in shared_dirs(root):
        src = os.path.join(repo, rel)
        if not os.path.exists(src):
            continue
        dst = os.path.join(wt, rel)
        if os.path.islink(dst):
            os.unlink(dst)
        elif os.path.exists(dst):
            continue  # real tracked content — leave it
        os.makedirs(os.path.dirname(dst) or wt, exist_ok=True)
        os.symlink(src, dst)


def max_trees(root=None):
    return config.get_int("POOL_MAX_TREES", r=root)


def _heal(state):
    state["entries"] = [e for e in state["entries"] if os.path.isdir(e["path"])]


def _next_name(state):
    used = {e["name"] for e in state["entries"]}
    i = 1
    while str(i) in used:
        i += 1
    return str(i)


def acquire(repo, holder, root=None):
    repo = os.path.abspath(repo)
    pdir = pool_dir(repo, root)
    branch = f"tokendance/{holder}"
    with state_lock(pdir):
        state = load_state(pdir)
        _heal(state)
        git(repo, "fetch", "origin", check=False)
        ref = default_ref(repo)
        slot = next((e for e in state["entries"]
                     if not e["leased"] and os.path.isdir(e["path"])
                     and not is_dirty(e["path"])), None)
        if slot is None:
            if len(state["entries"]) >= max_trees(root):
                raise RuntimeError(
                    f"pool full ({max_trees(root)} slots); no idle slot for {holder}")
            name = _next_name(state)
            path = os.path.join(pdir, name)
            add_worktree(repo, path, ref)
            slot = {"name": name, "path": path, "created_at": int(time.time()),
                    "leased": False, "lease_holder": ""}
            state["entries"].append(slot)
        reset_worktree(slot["path"], ref)
        git(slot["path"], "checkout", "-B", branch, ref)
        slot["leased"] = True
        slot["lease_holder"] = holder
        save_state(pdir, state)
        apply_shared_symlinks(repo, slot["path"], root)
        return slot["path"]


def release(repo, path, root=None):
    repo = os.path.abspath(repo)
    pdir = pool_dir(repo, root)
    with state_lock(pdir):
        state = load_state(pdir)
        ref = default_ref(repo)
        for e in state["entries"]:
            if os.path.abspath(e["path"]) == os.path.abspath(path):
                if os.path.isdir(e["path"]):
                    reset_worktree(e["path"], ref)
                e["leased"] = False
                e["lease_holder"] = ""
                break
        save_state(pdir, state)


def status(repo, root=None):
    pdir = pool_dir(os.path.abspath(repo), root)
    with state_lock(pdir):
        state = load_state(pdir)
        _heal(state)
        save_state(pdir, state)
    rows = []
    for e in state["entries"]:
        rows.append((e["name"], "leased" if e["leased"] else "idle",
                     e.get("lease_holder", ""), e["path"]))
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("acquire"); a.add_argument("--repo", required=True); a.add_argument("--holder", required=True)
    rl = sub.add_parser("release"); rl.add_argument("--repo", required=True); rl.add_argument("--path", required=True)
    st = sub.add_parser("status"); st.add_argument("--repo", required=True)
    args = ap.parse_args(argv)
    if args.cmd == "acquire":
        print(acquire(args.repo, args.holder, root=args.root))
    elif args.cmd == "release":
        release(args.repo, args.path, root=args.root)
    elif args.cmd == "status":
        for name, state_, holder, path in status(args.repo, root=args.root):
            print(f"{name}\t{state_}\t{holder}\t{path}")


if __name__ == "__main__":
    main()
