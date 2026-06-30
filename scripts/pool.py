#!/usr/bin/env python3
"""Warm worktree pool: lease/reuse git worktrees so parallel workers keep warm
build caches (per-slot target/) without re-paying full builds. Ported from
treehouse's lease-pool model; stdlib-only. State is flock-serialized JSON."""
import contextlib
import fcntl
import hashlib
import json
import os


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
