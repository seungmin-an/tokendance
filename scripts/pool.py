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


def _porcelain_path(line):
    """Repo-relative path out of one `status --porcelain` line ("?? .venv")."""
    p = line[3:]
    if " -> " in p:  # rename/copy: judge by the destination
        p = p.split(" -> ", 1)[1]
    if p.startswith('"') and p.endswith('"'):
        p = p[1:-1]
    return p.rstrip("/")


def is_dirty(wt, ignore=()):
    """True if the worktree holds work worth preserving.

    `ignore` lists repo-relative paths tokendance itself injects (see
    injected_paths): entries there — and their children — are our shares, not the
    user's work. Without that filter a repo which does not gitignore the share
    (npu-tools and .venv) reports `?? .venv` right after acquire wired it up, so
    the slot reads dirty forever and acquire never reuses it.
    """
    r = git(wt, "status", "--porcelain", "--untracked-files=all")
    ignored = [i.strip("/") for i in ignore if i.strip("/")]
    for line in r.stdout.splitlines():
        if not line.strip():
            continue
        p = _porcelain_path(line)
        if any(p == i or p.startswith(i + "/") for i in ignored):
            continue
        return True
    return False


def add_worktree(repo, path, ref):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    git(repo, "worktree", "add", "--detach", "--force", path, ref)


def reset_worktree(wt, ref):
    git(wt, "checkout", "--detach", "--force", ref)
    git(wt, "reset", "--hard", ref)
    # NOTE: no -x — preserves gitignored build state (target/), so acquire() can
    # reuse the cache it just leased; release() deletes target/ separately.
    # A share the repo does NOT gitignore (npu-tools and .venv) is untracked, so
    # this does remove it; acquire re-applies the shares right after, and
    # is_dirty() ignores them so a slot still carrying one stays reusable.
    git(wt, "clean", "-fd")


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


def worktree_manifest(repo):
    """<repo>/.tokendance-worktree.manifest entries: one repo-root-relative path
    per line (blank lines and #-comments ignored). Missing file → []."""
    path = os.path.join(repo, ".tokendance-worktree.manifest")
    try:
        with open(path) as f:
            return [ln.strip() for ln in f if ln.strip() and not ln.strip().startswith("#")]
    except FileNotFoundError:
        return []


def injected_paths(repo, root=None):
    """Repo-relative paths tokendance wires into every slot: the shared symlinks
    plus the manifest entries. is_dirty() must not count these as user work."""
    return set(shared_dirs(root)) | set(worktree_manifest(repo))


def apply_worktree_manifest(repo, wt):
    """Symlink heavy shared artifacts (e.g. dvc-extracted libtorch) from the main
    checkout into a worktree, per <repo>/.tokendance-worktree.manifest.

    Unlike apply_shared_symlinks (whole-dir, .venv-style), a manifest entry may
    already exist in the worktree as a git-checked-out directory mixed with
    gitignored content (e.g. artifacts/furiosa-libtorch has tracked *.dvc pointer
    files alongside gitignored current/jammy/noble extraction dirs). Whole-dir
    symlinking would fail there, so this does a one-level child merge instead:
    only children missing from the worktree side get linked; tracked children are
    left untouched. Missing manifest file → no-op (repos without one, including
    tokendance's own dogfood checkout, are unaffected)."""
    for rel in worktree_manifest(repo):
        src = os.path.join(repo, rel)
        if not os.path.exists(src):
            continue
        dst = os.path.join(wt, rel)
        if os.path.islink(dst):
            os.unlink(dst)
            os.symlink(src, dst)
            continue
        if not os.path.exists(dst):
            os.makedirs(os.path.dirname(dst) or wt, exist_ok=True)
            os.symlink(src, dst)
            continue
        if not os.path.isdir(dst) or not os.path.isdir(src):
            continue  # a real (non-symlink) file already there — leave it
        for name in os.listdir(src):
            child_dst = os.path.join(dst, name)
            if os.path.exists(child_dst) or os.path.islink(child_dst):
                continue  # already git-tracked (or previously linked) — don't touch
            os.symlink(os.path.join(src, name), child_dst)


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


def acquire(repo, holder, root=None, busy_paths=None):
    repo = os.path.abspath(repo)
    pdir = pool_dir(repo, root)
    branch = f"tokendance/{holder}"
    # Slots whose path a live session still occupies (e.g. a human-driven td-*
    # tmux window): never reuse them, git worktree allows one checkout per slot.
    busy = {os.path.abspath(p) for p in (busy_paths or [])}
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
            apply_worktree_manifest(repo, owned["path"])
            return owned["path"]
        git(repo, "fetch", "origin", check=False)
        ref = default_ref(repo)
        ignore = injected_paths(repo, root)
        slot = next((e for e in state["entries"]
                     if not e["leased"] and os.path.isdir(e["path"])
                     and os.path.abspath(e["path"]) not in busy
                     and not is_dirty(e["path"], ignore)), None)
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
        apply_worktree_manifest(repo, slot["path"])
        return slot["path"]


def release(repo, path, root=None, *, expected_holder=None, busy_paths=None):
    """Free a slot's lease, reset its worktree and drop its build cache.

    The reset leaves gitignored `target/` in place (see reset_worktree); we then
    delete it, because a released slot has no holder left to reuse the cache and
    it grows without bound otherwise — one npu-tools slot reached 115G.

    `busy_paths` lists slot paths a live session still occupies (same contract as
    acquire). For those the lease is freed but the worktree is left exactly as it
    is: reset_worktree force-checks-out the base ref and runs `clean -fd`, which
    under a live human session yanks HEAD off the branch they are working on and
    deletes their untracked files. acquire already refuses to hand out a busy
    slot, so freeing the lease alone cannot let anyone else in.
    """
    repo = os.path.abspath(repo)
    pdir = pool_dir(repo, root)
    busy = {os.path.abspath(p) for p in (busy_paths or [])}
    with state_lock(pdir):
        state = load_state(pdir)
        ref = default_ref(repo)
        for e in state["entries"]:
            if os.path.abspath(e["path"]) == os.path.abspath(path):
                if expected_holder is not None and e.get("lease_holder", "") != expected_holder:
                    return  # stale snapshot: slot re-acquired by someone else; leave it
                if os.path.abspath(e["path"]) in busy:
                    print(f"[pool] release: {e['path']} is occupied by a live session — "
                          f"freeing the lease without resetting the worktree", file=sys.stderr)
                elif os.path.isdir(e["path"]):
                    reset_worktree(e["path"], ref)
                    # reset_worktree's `clean -fd` has no -x, so the gitignored
                    # build cache survives it. A released slot has no holder to
                    # reuse the cache, and one npu-tools slot reached 115G, so
                    # drop it here. Only on release: acquire() also calls
                    # reset_worktree and must keep the cache it just leased.
                    shutil.rmtree(target_dir(e), ignore_errors=True)
                e["leased"] = False
                e["lease_holder"] = ""
                break
        else:
            print(f"[pool] release: path not in pool: {path}", file=sys.stderr)
        save_state(pdir, state)


def reclaim_stale(repo, root=None, *, keep_holders, busy_paths=None):
    """Release leased slots whose holder is not in keep_holders. Returns reclaimed holders.

    keep_holders is the set of holder ids the CALLER deems still alive (fresh heartbeat /
    active task). pool.py stays free of task-state knowledge — morning.py computes the set.
    busy_paths is forwarded to release(): an orphan-looking lease may still have a live
    human session sitting in its worktree, and reclaiming it must not reset that tree."""
    repo = os.path.abspath(repo)
    pdir = pool_dir(repo, root)
    with state_lock(pdir):
        state = load_state(pdir)
        stale = [e for e in state["entries"]
                 if e["leased"] and e.get("lease_holder", "") not in keep_holders]
    reclaimed = []
    for e in stale:
        release(repo, e["path"], root, expected_holder=e["lease_holder"],
                busy_paths=busy_paths)   # release takes its own lock; TOCTOU-safe
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


def gc_targets(repo, root=None, *, now=None, dry_run=False, busy_paths=None):
    """Evict slot target/ dirs: tier 1 by idle age, tier 2 by size cap (coldest first).

    `busy_paths` lists slot paths a live session still occupies (same contract as
    acquire/release). Such a slot can be unleased — a human's td-* session outlives
    its lease — and evicting its target/ deletes a build cache out from under them,
    so it is excluded from the candidate set exactly like a leased slot.
    """
    if now is None:
        now = time.time()
    repo = os.path.abspath(repo)
    pdir = pool_dir(repo, root)
    idle_days = config.get_int("POOL_TARGET_IDLE_DAYS", r=root)
    max_gb = _cfg_float("POOL_TARGET_MAX_GB", root)
    low_gb = _cfg_float("POOL_TARGET_LOWWATER_GB", root) or (max_gb * 0.8)
    busy = {os.path.abspath(p) for p in (busy_paths or [])}
    acts = []
    with state_lock(pdir):
        state = load_state(pdir)
        _heal(repo, pdir, state)
        # idle (unleased, unoccupied) slots with an existing target/, in state order
        cand = []
        for e in state["entries"]:
            if e["leased"] or os.path.abspath(e["path"]) in busy:
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
    a.add_argument("--busy-path", action="append", default=None,
                   help="a slot path a live session occupies; never reuse it (repeatable)")
    rl = sub.add_parser("release"); rl.add_argument("--repo", required=True); rl.add_argument("--path", required=True)
    rl.add_argument("--expected-holder", default=None)
    rl.add_argument("--busy-path", action="append", default=None,
                    help="a slot path a live session occupies; free the lease but do not "
                         "reset its worktree (repeatable)")
    st = sub.add_parser("status"); st.add_argument("--repo", required=True)
    dk = sub.add_parser("disk"); dk.add_argument("--repo", required=True)
    gt = sub.add_parser("gc-targets"); gt.add_argument("--repo", required=True)
    gt.add_argument("--dry-run", action="store_true")
    gt.add_argument("--busy-path", action="append", default=None,
                    help="a slot path a live session occupies; never evict its "
                         "target/ (repeatable)")
    args = ap.parse_args(argv)
    if args.cmd == "acquire":
        print(acquire(args.repo, args.holder, root=args.root, busy_paths=args.busy_path))
    elif args.cmd == "release":
        release(args.repo, args.path, root=args.root, expected_holder=args.expected_holder,
                busy_paths=args.busy_path)
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
        acts = gc_targets(args.repo, root=args.root, dry_run=args.dry_run,
                          busy_paths=args.busy_path)
        freed = 0
        for a in acts:
            print(f"{a['name']}\t{a['reason']}\t{a['freed_bytes']}")
            freed += a["freed_bytes"]
        print(f"FREED\t{freed}")


if __name__ == "__main__":
    main()
