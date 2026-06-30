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
import shutil
import subprocess
import sys
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


def _heal(repo, pdir, state):
    # Drop state entries whose worktree dir is gone.
    state["entries"] = [e for e in state["entries"] if os.path.isdir(e["path"])]
    # Reconcile the filesystem: a crash between add_worktree and save_state can
    # leave an orphan slot dir (on disk + git-registered) absent from state.json.
    # Without removing it, _next_name re-picks its name and `git worktree add`
    # fails (exit 128), wedging all future acquires. Remove orphans so re-create
    # is clean.
    git(repo, "worktree", "prune", check=False)
    known = {os.path.abspath(e["path"]) for e in state["entries"]}
    if os.path.isdir(pdir):
        for name in sorted(os.listdir(pdir)):
            p = os.path.join(pdir, name)
            if not os.path.isdir(p) or os.path.abspath(p) in known:
                continue  # skip state.json/.lock files and known slots
            git(repo, "worktree", "remove", "--force", p, check=False)
            shutil.rmtree(p, ignore_errors=True)
    git(repo, "worktree", "prune", check=False)


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
        _heal(repo, pdir, state)
        # Idempotent per holder: if this holder already owns a live slot, return it
        # as-is (preserve in-progress work; branch already checked out). Without this,
        # launch-worker re-running prepare-worktree on --resume would leak the prior
        # slot and reset the worker's working tree.
        owned = next((e for e in state["entries"]
                      if e["leased"] and e["lease_holder"] == holder
                      and os.path.isdir(e["path"])), None)
        if owned is not None:
            apply_shared_symlinks(repo, owned["path"], root)
            return owned["path"]
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
        else:
            print(f"[pool] release: path not in pool: {path}", file=sys.stderr)
        save_state(pdir, state)


def reclaim_stale(repo, root=None, *, keep_holders):
    """Release leased slots whose holder is not in keep_holders. Returns reclaimed holders.

    keep_holders is the set of holder ids the CALLER deems still alive (fresh heartbeat /
    active task). pool.py stays free of task-state knowledge — morning.py computes the set."""
    repo = os.path.abspath(repo)
    pdir = pool_dir(repo, root)
    with state_lock(pdir):
        state = load_state(pdir)
        stale = [e for e in state["entries"]
                 if e["leased"] and e.get("lease_holder", "") not in keep_holders]
    reclaimed = []
    for e in stale:
        release(repo, e["path"], root)   # release takes its own lock; reset + clear lease
        reclaimed.append(e["lease_holder"])
    return reclaimed


def target_dir(entry):
    return os.path.join(entry["path"], "target")


def dir_size(path):
    total = 0
    for dirpath, dirnames, filenames in os.walk(path):  # os.walk does not follow symlinks
        for name in filenames:
            fp = os.path.join(dirpath, name)
            try:
                if not os.path.islink(fp):
                    total += os.path.getsize(fp)
            except OSError:
                pass
    return total


def disk_report(repo, root=None):
    repo = os.path.abspath(repo)
    pdir = pool_dir(repo, root)
    with state_lock(pdir):
        state = load_state(pdir)
        _heal(repo, pdir, state)
        save_state(pdir, state)
    slots, total = [], 0
    for e in state["entries"]:
        td = target_dir(e)
        b = dir_size(td)
        total += b
        mtime = int(os.path.getmtime(td)) if os.path.isdir(td) else 0
        slots.append({"name": e["name"], "leased": e["leased"],
                      "holder": e.get("lease_holder", ""),
                      "target_bytes": b, "target_mtime": mtime, "path": e["path"]})
    return {"slots": slots, "total_bytes": total}


def _cfg_float(key, root=None):
    try:
        return float(config.get(key, "0", r=root))
    except (TypeError, ValueError):
        return 0.0


def _evict_target(slot_path, idle_days, root=None):
    td = os.path.join(slot_path, "target")
    if not os.path.isdir(td):
        return 0
    freed = dir_size(td)
    use_sweep = config.get("POOL_TARGET_USE_CARGO_SWEEP", "0", r=root) == "1"
    if use_sweep and shutil.which("cargo-sweep"):
        r = subprocess.run(["cargo", "sweep", "--time", str(max(1, idle_days))],
                           cwd=slot_path, capture_output=True, text=True)
        if r.returncode == 0:
            return freed - dir_size(td)  # bytes actually reclaimed
    shutil.rmtree(td, ignore_errors=True)
    return freed


def gc_targets(repo, root=None, *, now=None, dry_run=False):
    if now is None:
        now = time.time()
    repo = os.path.abspath(repo)
    pdir = pool_dir(repo, root)
    idle_days = config.get_int("POOL_TARGET_IDLE_DAYS", r=root)
    max_gb = _cfg_float("POOL_TARGET_MAX_GB", root)
    low_gb = _cfg_float("POOL_TARGET_LOWWATER_GB", root) or (max_gb * 0.8)
    acts = []
    with state_lock(pdir):
        state = load_state(pdir)
        _heal(repo, pdir, state)
        # idle (unleased) slots with an existing target/, newest-first by mtime
        cand = []
        for e in state["entries"]:
            if e["leased"]:
                continue
            td = target_dir(e)
            if not os.path.isdir(td):
                continue
            cand.append((e, dir_size(td), os.path.getmtime(td)))
        evicted = set()
        # Tier 1 — idle sweep
        if idle_days > 0:
            cutoff = now - idle_days * 86400
            for e, b, mt in cand:
                if mt <= cutoff:
                    acts.append({"name": e["name"], "reason": f"idle ≥ {idle_days}d", "freed_bytes": b})
                    if not dry_run:
                        _evict_target(e["path"], idle_days, root)
                    evicted.add(e["name"])
        # Tier 2 — size-cap LRU backstop
        if max_gb > 0:
            total = sum(b for e, b, _ in cand if e["name"] not in evicted)
            cap = int(max_gb * 1024**3)
            low = int(low_gb * 1024**3)
            if total > cap:
                # coldest (oldest mtime) first
                for e, b, mt in sorted((c for c in cand if c[0]["name"] not in evicted),
                                       key=lambda c: c[2]):
                    if total <= low:
                        break
                    acts.append({"name": e["name"], "reason": "size-cap LRU", "freed_bytes": b})
                    if not dry_run:
                        _evict_target(e["path"], idle_days, root)
                    evicted.add(e["name"])
                    total -= b
        save_state(pdir, state)  # entries unchanged; heal may have pruned orphans
    return acts


def status(repo, root=None):
    pdir = pool_dir(os.path.abspath(repo), root)
    with state_lock(pdir):
        state = load_state(pdir)
        _heal(os.path.abspath(repo), pdir, state)
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
    dk = sub.add_parser("disk"); dk.add_argument("--repo", required=True)
    gt = sub.add_parser("gc-targets"); gt.add_argument("--repo", required=True)
    gt.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    if args.cmd == "acquire":
        print(acquire(args.repo, args.holder, root=args.root))
    elif args.cmd == "release":
        release(args.repo, args.path, root=args.root)
    elif args.cmd == "status":
        for name, state_, holder, path in status(args.repo, root=args.root):
            print(f"{name}\t{state_}\t{holder}\t{path}")
    elif args.cmd == "disk":
        rep = disk_report(args.repo, root=args.root)
        for s in rep["slots"]:
            print(f"{s['name']}\t{'leased' if s['leased'] else 'idle'}\t"
                  f"{s['holder']}\t{s['target_bytes']}\t{s['path']}")
        print(f"TOTAL\t{rep['total_bytes']}")
    elif args.cmd == "gc-targets":
        acts = gc_targets(args.repo, root=args.root, dry_run=args.dry_run)
        freed = 0
        for a in acts:
            print(f"{a['name']}\t{a['reason']}\t{a['freed_bytes']}")
            freed += a["freed_bytes"]
        print(f"FREED\t{freed}")


if __name__ == "__main__":
    main()
