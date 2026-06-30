#!/usr/bin/env python3
"""Warm worktree pool: lease/reuse git worktrees so parallel workers keep warm
build caches (per-slot target/) without re-paying full builds. Ported from
treehouse's lease-pool model; stdlib-only. State is flock-serialized JSON."""
import contextlib
import fcntl
import hashlib
import json
import os
import subprocess

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
