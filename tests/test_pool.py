# tests/test_pool.py
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from scripts import pool


def _init_repo(path):
    os.makedirs(path, exist_ok=True)
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    run = lambda *a: subprocess.run(["git", "-C", path, *a], check=True,
                                    capture_output=True, text=True, env=env)
    run("init", "-q", "-b", "main")
    with open(os.path.join(path, "README"), "w") as f:
        f.write("hi\n")
    with open(os.path.join(path, ".gitignore"), "w") as f:
        f.write("target/\n.venv\n")
    run("add", "-A")
    run("commit", "-q", "-m", "init")
    return path


class StateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = os.path.join(self.tmp, "myrepo")
        os.makedirs(self.repo)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_pool_dir_is_stable_and_repo_keyed(self):
        d1 = pool.pool_dir(self.repo, root=self.tmp)
        d2 = pool.pool_dir(self.repo, root=self.tmp)
        self.assertEqual(d1, d2)
        self.assertTrue(d1.startswith(os.path.join(self.tmp, "state", "pool")))
        self.assertIn("myrepo-", os.path.basename(d1))

    def test_save_then_load_roundtrips(self):
        pdir = pool.pool_dir(self.repo, root=self.tmp)
        state = {"entries": [{"name": "1", "path": "/x", "created_at": 5,
                              "leased": True, "lease_holder": "t1"}]}
        pool.save_state(pdir, state)
        self.assertEqual(pool.load_state(pdir), state)

    def test_load_missing_returns_empty(self):
        pdir = pool.pool_dir(self.repo, root=self.tmp)
        self.assertEqual(pool.load_state(pdir), {"entries": []})

    def test_state_lock_serializes_writes(self):
        pdir = pool.pool_dir(self.repo, root=self.tmp)
        with pool.state_lock(pdir):
            pool.save_state(pdir, {"entries": [{"name": "1"}]})
        self.assertEqual(len(pool.load_state(pdir)["entries"]), 1)


class GitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = _init_repo(os.path.join(self.tmp, "repo"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_default_ref_returns_head_without_origin(self):
        ref = pool.default_ref(self.repo)
        self.assertTrue(ref)  # a sha or refname

    def test_is_dirty_detects_untracked(self):
        self.assertFalse(pool.is_dirty(self.repo))
        with open(os.path.join(self.repo, "new.txt"), "w") as f:
            f.write("x")
        self.assertTrue(pool.is_dirty(self.repo))

    def test_add_then_reset_preserves_gitignored(self):
        wt = os.path.join(self.tmp, "wt1")
        ref = pool.default_ref(self.repo)
        pool.add_worktree(self.repo, wt, ref)
        # simulate a warm gitignored build dir + an untracked tracked-area file
        os.makedirs(os.path.join(wt, "target"))
        with open(os.path.join(wt, "target", "cache.bin"), "w") as f:
            f.write("warm")
        with open(os.path.join(wt, "scratch.txt"), "w") as f:
            f.write("dirty")
        pool.reset_worktree(wt, ref)
        # gitignored target/ survives (no -x); non-ignored scratch is cleaned
        self.assertTrue(os.path.exists(os.path.join(wt, "target", "cache.bin")))
        self.assertFalse(os.path.exists(os.path.join(wt, "scratch.txt")))
        self.assertFalse(pool.is_dirty(wt))


class SymlinkTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(self.repo, ".venv", "bin"))
        self.wt = os.path.join(self.tmp, "wt")
        os.makedirs(self.wt)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_symlinks_existing_shared_dir(self):
        pool.apply_shared_symlinks(self.repo, self.wt, root=self.tmp)
        link = os.path.join(self.wt, ".venv")
        self.assertTrue(os.path.islink(link))
        self.assertEqual(os.path.realpath(link),
                         os.path.realpath(os.path.join(self.repo, ".venv")))

    def test_skips_missing_shared_dir(self):
        # no target/ in repo → no link created, no error
        pool.apply_shared_symlinks(self.repo, self.wt, root=self.tmp)
        self.assertFalse(os.path.exists(os.path.join(self.wt, "target")))

    def test_replaces_stale_link(self):
        os.symlink("/nonexistent", os.path.join(self.wt, ".venv"))
        pool.apply_shared_symlinks(self.repo, self.wt, root=self.tmp)
        self.assertEqual(os.path.realpath(os.path.join(self.wt, ".venv")),
                         os.path.realpath(os.path.join(self.repo, ".venv")))


class ManifestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        self.wt = os.path.join(self.tmp, "wt")
        os.makedirs(self.wt)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_manifest(self, *lines):
        with open(os.path.join(self.repo, ".tokendance-worktree.manifest"), "w") as f:
            f.write("\n".join(lines) + "\n")

    def test_no_manifest_file_is_a_noop(self):
        pool.apply_worktree_manifest(self.repo, self.wt)
        self.assertEqual(os.listdir(self.wt), [])

    def test_missing_worktree_path_symlinks_whole_dir(self):
        os.makedirs(os.path.join(self.repo, "artifacts", "libtorch", "current"))
        self._write_manifest("artifacts/libtorch")
        pool.apply_worktree_manifest(self.repo, self.wt)
        link = os.path.join(self.wt, "artifacts", "libtorch")
        self.assertTrue(os.path.islink(link))
        self.assertEqual(os.path.realpath(link),
                         os.path.realpath(os.path.join(self.repo, "artifacts", "libtorch")))

    def test_existing_dir_gets_child_level_merge(self):
        # main checkout has the dvc pointer + the extracted (gitignored) dir
        src = os.path.join(self.repo, "artifacts", "libtorch")
        os.makedirs(src)
        with open(os.path.join(src, "libtorch.dvc"), "w") as f:
            f.write("dvc-pointer")
        os.makedirs(os.path.join(src, "current", "lib"))
        # worktree already has the dir via git checkout, with only the tracked pointer
        dst = os.path.join(self.wt, "artifacts", "libtorch")
        os.makedirs(dst)
        with open(os.path.join(dst, "libtorch.dvc"), "w") as f:
            f.write("dvc-pointer")  # tracked — must stay a real file, not linked over
        self._write_manifest("artifacts/libtorch")
        pool.apply_worktree_manifest(self.repo, self.wt)
        self.assertFalse(os.path.islink(os.path.join(dst, "libtorch.dvc")))
        current_link = os.path.join(dst, "current")
        self.assertTrue(os.path.islink(current_link))
        self.assertEqual(os.path.realpath(current_link),
                         os.path.realpath(os.path.join(src, "current")))

    def test_missing_source_path_skipped_without_error(self):
        self._write_manifest("artifacts/does-not-exist")
        pool.apply_worktree_manifest(self.repo, self.wt)  # must not raise
        self.assertFalse(os.path.exists(os.path.join(self.wt, "artifacts")))

    def test_blank_lines_and_comments_ignored(self):
        os.makedirs(os.path.join(self.repo, "cache"))
        self._write_manifest("", "# a comment", "cache", "")
        pool.apply_worktree_manifest(self.repo, self.wt)
        self.assertTrue(os.path.islink(os.path.join(self.wt, "cache")))

    def test_acquire_applies_manifest_on_new_and_reacquired_slot(self):
        repo = _init_repo(self.repo)
        src = os.path.join(repo, "artifacts", "libtorch")
        os.makedirs(os.path.join(src, "current"))
        with open(os.path.join(repo, ".tokendance-worktree.manifest"), "w") as f:
            f.write("artifacts/libtorch\n")
        p1 = pool.acquire(repo, "task-1", root=self.tmp)
        link = os.path.join(p1, "artifacts", "libtorch")
        self.assertTrue(os.path.islink(link))
        # idempotent-owner reacquire path also (re-)applies the manifest
        p2 = pool.acquire(repo, "task-1", root=self.tmp)
        self.assertEqual(p1, p2)
        self.assertTrue(os.path.islink(os.path.join(p2, "artifacts", "libtorch")))


class ReleaseBusyTest(unittest.TestCase):
    """release() must not reset a worktree a live session still occupies.

    acquire has skipped busy slots since 2026-07-13, but release resets the
    worktree unconditionally — so a stale-lease GC (or a reclaim carrying a stale
    worktree.path) force-checks-out the base ref under a human's live session:
    their HEAD moves off the branch they were on and `clean -fd` takes untracked
    files with it. Reported 2026-07-27 ("점유중인 애의 head 가 바뀌면 안되는거잖아");
    the slot-1 reflog shows it on 07-13 and again on 07-19.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = _init_repo(os.path.join(self.tmp, "repo"))
        self.wt = pool.acquire(self.repo, "holder-1", root=self.tmp)
        subprocess.run(["git", "-C", self.wt, "checkout", "-q", "-B", "mywork"],
                       check=True, capture_output=True)
        with open(os.path.join(self.wt, "scratch.txt"), "w") as f:
            f.write("work in progress")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _head(self):
        return subprocess.run(["git", "-C", self.wt, "rev-parse", "--abbrev-ref", "HEAD"],
                              capture_output=True, text=True).stdout.strip()

    def test_release_of_a_busy_slot_leaves_the_worktree_alone(self):
        pool.release(self.repo, self.wt, root=self.tmp, busy_paths=[self.wt])
        self.assertEqual(self._head(), "mywork")                               # HEAD untouched
        self.assertTrue(os.path.exists(os.path.join(self.wt, "scratch.txt")))  # work untouched
        entry = pool.load_state(pool.pool_dir(self.repo, self.tmp))["entries"][0]
        self.assertFalse(entry["leased"])  # lease still freed — only the reset is skipped

    def test_release_of_an_idle_slot_still_resets(self):
        pool.release(self.repo, self.wt, root=self.tmp)
        self.assertEqual(self._head(), "HEAD")                                 # detached at base
        self.assertFalse(os.path.exists(os.path.join(self.wt, "scratch.txt")))

    def test_reclaim_stale_forwards_busy_paths(self):
        pool.reclaim_stale(self.repo, root=self.tmp, keep_holders=set(),
                           busy_paths=[self.wt])
        self.assertEqual(self._head(), "mywork")
        self.assertTrue(os.path.exists(os.path.join(self.wt, "scratch.txt")))


class DirtyIgnoreTest(unittest.TestCase):
    """A slot must not look dirty because of the shares tokendance itself injects.

    npu-tools does not gitignore .venv, so the symlink apply_shared_symlinks
    creates at acquire shows up as `?? .venv` — and acquire skips dirty slots, so
    that slot was never reusable again ("pool full" with idle slots). The repos in
    the other fixtures gitignore .venv, which is why this stayed invisible.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = _init_repo(os.path.join(self.tmp, "repo"))
        self._commit_gitignore("target/\n")   # mirror npu-tools: .venv NOT ignored
        os.makedirs(os.path.join(self.repo, ".venv", "bin"))
        self.wt = os.path.join(self.tmp, "wt")
        pool.add_worktree(self.repo, self.wt, pool.default_ref(self.repo))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _commit_gitignore(self, body):
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
        with open(os.path.join(self.repo, ".gitignore"), "w") as f:
            f.write(body)
        for a in (("add", "-A"), ("commit", "-q", "-m", "gitignore")):
            subprocess.run(["git", "-C", self.repo, *a], check=True,
                           capture_output=True, text=True, env=env)

    def test_injected_share_alone_is_not_dirty(self):
        pool.apply_shared_symlinks(self.repo, self.wt, root=self.tmp)
        self.assertTrue(pool.is_dirty(self.wt))                 # untracked to git…
        self.assertFalse(pool.is_dirty(self.wt, [".venv"]))     # …but ours, not user work

    def test_real_user_work_is_still_dirty(self):
        pool.apply_shared_symlinks(self.repo, self.wt, root=self.tmp)
        with open(os.path.join(self.wt, "scratch.txt"), "w") as f:
            f.write("real work")
        self.assertTrue(pool.is_dirty(self.wt, [".venv"]))

    def test_manifest_child_links_are_ignored(self):
        # tracked dir whose gitignored children get linked by the child-level merge
        src = os.path.join(self.repo, "artifacts", "lt")
        os.makedirs(src)
        with open(os.path.join(src, "lt.dvc"), "w") as f:
            f.write("pointer")
        with open(os.path.join(self.repo, ".tokendance-worktree.manifest"), "w") as f:
            f.write("artifacts/lt\n")
        self._commit_gitignore("target/\n")   # picks up artifacts/lt/lt.dvc too
        os.makedirs(os.path.join(src, "current"))
        wt2 = os.path.join(self.tmp, "wt2")
        pool.add_worktree(self.repo, wt2, pool.default_ref(self.repo))
        pool.apply_worktree_manifest(self.repo, wt2)
        self.assertTrue(os.path.islink(os.path.join(wt2, "artifacts", "lt", "current")))
        self.assertTrue(pool.is_dirty(wt2))
        self.assertFalse(pool.is_dirty(wt2, ["artifacts/lt"]))

    def test_injected_paths_unions_shares_and_manifest(self):
        with open(os.path.join(self.repo, ".tokendance-worktree.manifest"), "w") as f:
            f.write("artifacts/lt\n")
        self.assertEqual(pool.injected_paths(self.repo, root=self.tmp),
                         {".venv", "artifacts/lt"})

    def test_acquire_reuses_a_released_slot_that_kept_the_share(self):
        p1 = pool.acquire(self.repo, "task-1", root=self.tmp)
        pool.release(self.repo, p1, root=self.tmp)
        # release resets the slot; re-inject the share as a mid-lease clean would leave it
        pool.apply_shared_symlinks(self.repo, p1, root=self.tmp)
        self.assertEqual(pool.acquire(self.repo, "task-2", root=self.tmp), p1)


def _branch(wt):
    return subprocess.run(["git", "-C", wt, "rev-parse", "--abbrev-ref", "HEAD"],
                          capture_output=True, text=True).stdout.strip()


class AcquireReleaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = _init_repo(os.path.join(self.tmp, "repo"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_acquire_creates_slot_on_task_branch(self):
        path = pool.acquire(self.repo, "task-1", root=self.tmp)
        self.assertTrue(os.path.isdir(path))
        self.assertEqual(_branch(path), "tokendance/task-1")
        st = pool.load_state(pool.pool_dir(self.repo, self.tmp))
        self.assertTrue(st["entries"][0]["leased"])
        self.assertEqual(st["entries"][0]["lease_holder"], "task-1")

    def test_release_then_reacquire_reuses_same_slot(self):
        p1 = pool.acquire(self.repo, "task-1", root=self.tmp)
        pool.release(self.repo, p1, root=self.tmp)
        p2 = pool.acquire(self.repo, "task-2", root=self.tmp)
        self.assertEqual(p1, p2)  # warm reuse
        self.assertEqual(_branch(p2), "tokendance/task-2")
        st = pool.load_state(pool.pool_dir(self.repo, self.tmp))
        self.assertEqual(len(st["entries"]), 1)

    def test_release_expected_holder_mismatch_is_noop(self):
        # A stale caller (e.g. reclaim of a task whose worktree.path duplicates a
        # slot re-acquired by someone else) must not free the current holder's lease.
        p1 = pool.acquire(self.repo, "task-1", root=self.tmp)
        pool.release(self.repo, p1, root=self.tmp, expected_holder="task-OTHER")
        st = pool.load_state(pool.pool_dir(self.repo, self.tmp))
        self.assertTrue(st["entries"][0]["leased"])
        self.assertEqual(st["entries"][0]["lease_holder"], "task-1")

    def test_release_expected_holder_match_frees_slot(self):
        p1 = pool.acquire(self.repo, "task-1", root=self.tmp)
        pool.release(self.repo, p1, root=self.tmp, expected_holder="task-1")
        st = pool.load_state(pool.pool_dir(self.repo, self.tmp))
        self.assertFalse(st["entries"][0]["leased"])

    def test_parallel_leases_get_distinct_slots(self):
        p1 = pool.acquire(self.repo, "task-1", root=self.tmp)
        p2 = pool.acquire(self.repo, "task-2", root=self.tmp)
        self.assertNotEqual(p1, p2)

    def test_acquire_skips_busy_slot_and_makes_new(self):
        # An idle slot whose path a live session still occupies must NOT be reused;
        # acquire creates a fresh slot instead (git worktree = one checkout per slot).
        p1 = pool.acquire(self.repo, "task-1", root=self.tmp)
        pool.release(self.repo, p1, root=self.tmp)
        p2 = pool.acquire(self.repo, "task-2", root=self.tmp, busy_paths={p1})
        self.assertNotEqual(p1, p2)
        st = pool.load_state(pool.pool_dir(self.repo, self.tmp))
        self.assertEqual(len(st["entries"]), 2)   # busy slot left alone, new one added

    def test_acquire_busy_path_not_matching_reuses_idle(self):
        p1 = pool.acquire(self.repo, "task-1", root=self.tmp)
        pool.release(self.repo, p1, root=self.tmp)
        p2 = pool.acquire(self.repo, "task-2", root=self.tmp,
                          busy_paths={"/some/other/path"})
        self.assertEqual(p1, p2)   # busy set irrelevant → warm reuse

    def test_acquire_busy_only_slot_at_cap_raises(self):
        with open(os.path.join(self.tmp, "config.local.md"), "w") as f:
            f.write("POOL_MAX_TREES=1\n")
        p1 = pool.acquire(self.repo, "task-1", root=self.tmp)
        pool.release(self.repo, p1, root=self.tmp)
        with self.assertRaises(RuntimeError):   # only slot is busy, can't grow → no slot
            pool.acquire(self.repo, "task-2", root=self.tmp, busy_paths={p1})

    def test_acquire_busy_paths_none_is_default_behavior(self):
        p1 = pool.acquire(self.repo, "task-1", root=self.tmp)
        pool.release(self.repo, p1, root=self.tmp)
        p2 = pool.acquire(self.repo, "task-2", root=self.tmp)   # busy_paths omitted
        self.assertEqual(p1, p2)

    def test_pool_full_raises(self):
        # cap the pool at 1 via config.local.md
        with open(os.path.join(self.tmp, "config.local.md"), "w") as f:
            f.write("POOL_MAX_TREES=1\n")
        pool.acquire(self.repo, "task-1", root=self.tmp)
        with self.assertRaises(RuntimeError):
            pool.acquire(self.repo, "task-2", root=self.tmp)

    def test_reacquire_same_holder_is_idempotent(self):
        p1 = pool.acquire(self.repo, "task-1", root=self.tmp)
        marker = os.path.join(p1, "in_progress.txt")
        with open(marker, "w") as f:
            f.write("wip")
        p2 = pool.acquire(self.repo, "task-1", root=self.tmp)
        self.assertEqual(p1, p2)                # same slot, not a new lease
        self.assertTrue(os.path.exists(marker)) # not reset — in-progress work preserved
        st = pool.load_state(pool.pool_dir(self.repo, self.tmp))
        self.assertEqual(len(st["entries"]), 1) # no second slot leaked

    def test_release_drops_warm_target(self):
        # 2026-09-01 contract change: a released slot has no holder left to reuse
        # its build cache, and one npu-tools slot reached 115G, so release drops
        # it. Replaces test_warm_target_survives_release, which asserted the
        # opposite carry-over-the-cache behaviour.
        p1 = pool.acquire(self.repo, "task-1", root=self.tmp)
        os.makedirs(os.path.join(p1, "target"))
        with open(os.path.join(p1, "target", "warm.bin"), "w") as f:
            f.write("cache")
        pool.release(self.repo, p1, root=self.tmp)
        self.assertFalse(os.path.exists(os.path.join(p1, "target")))
        p2 = pool.acquire(self.repo, "task-2", root=self.tmp)
        self.assertEqual(p1, p2)  # still reuses the same (only idle) slot

    def test_busy_slot_keeps_warm_target_on_release(self):
        # A live session may be mid-build: free the lease, leave its tree — and
        # its cache — alone. Same guard that stops us resetting a human's HEAD.
        p1 = pool.acquire(self.repo, "task-1", root=self.tmp)
        os.makedirs(os.path.join(p1, "target"))
        with open(os.path.join(p1, "target", "warm.bin"), "w") as f:
            f.write("cache")
        pool.release(self.repo, p1, root=self.tmp, busy_paths=[p1])
        self.assertTrue(os.path.exists(os.path.join(p1, "target", "warm.bin")))

    def test_heal_reconciles_orphan_slot_after_lost_state(self):
        # Create slot "1", release it (dir + git registration remain), then wipe
        # state.json to simulate a crash before save. Next acquire must NOT raise.
        p1 = pool.acquire(self.repo, "task-1", root=self.tmp)
        pool.release(self.repo, p1, root=self.tmp)
        os.remove(os.path.join(pool.pool_dir(self.repo, self.tmp), "state.json"))
        p2 = pool.acquire(self.repo, "task-2", root=self.tmp)   # must not raise exit-128
        self.assertTrue(os.path.isdir(p2))
        self.assertEqual(_branch(p2), "tokendance/task-2")


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = _init_repo(os.path.join(self.tmp, "repo"))
        self.script = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                                   "scripts", "pool.py")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, *args):
        return subprocess.run([sys.executable, self.script, "--root", self.tmp, *args],
                              capture_output=True, text=True)

    def test_cli_acquire_prints_path_and_status_lists_it(self):
        r = self._run("acquire", "--repo", self.repo, "--holder", "task-1")
        self.assertEqual(r.returncode, 0, r.stderr)
        path = r.stdout.strip().splitlines()[-1]
        self.assertTrue(os.path.isdir(path))
        s = self._run("status", "--repo", self.repo)
        self.assertIn("task-1", s.stdout)
        self.assertIn("leased", s.stdout)

    def test_cli_acquire_busy_path_skips_slot(self):
        p1 = self._run("acquire", "--repo", self.repo, "--holder", "task-1").stdout.strip().splitlines()[-1]
        self._run("release", "--repo", self.repo, "--path", p1)
        r = self._run("acquire", "--repo", self.repo, "--holder", "task-2", "--busy-path", p1)
        self.assertEqual(r.returncode, 0, r.stderr)   # option must be recognized
        p2 = r.stdout.strip().splitlines()[-1]
        self.assertNotEqual(p1, p2)   # busy slot skipped → fresh slot

    def test_cli_release_frees_slot(self):
        p = self._run("acquire", "--repo", self.repo, "--holder", "task-1").stdout.strip().splitlines()[-1]
        self._run("release", "--repo", self.repo, "--path", p)
        s = self._run("status", "--repo", self.repo)
        self.assertIn("idle", s.stdout)

    def test_cli_release_expected_holder_mismatch_is_noop(self):
        p = self._run("acquire", "--repo", self.repo, "--holder", "task-1").stdout.strip().splitlines()[-1]
        r = self._run("release", "--repo", self.repo, "--path", p, "--expected-holder", "task-OTHER")
        self.assertEqual(r.returncode, 0, r.stderr)   # option must be recognized
        s = self._run("status", "--repo", self.repo)
        self.assertIn("leased", s.stdout)
        self.assertIn("task-1", s.stdout)

    def test_cli_release_expected_holder_match_frees_slot(self):
        p = self._run("acquire", "--repo", self.repo, "--holder", "task-1").stdout.strip().splitlines()[-1]
        r = self._run("release", "--repo", self.repo, "--path", p, "--expected-holder", "task-1")
        self.assertEqual(r.returncode, 0, r.stderr)
        s = self._run("status", "--repo", self.repo)
        self.assertIn("idle", s.stdout)


if __name__ == "__main__":
    unittest.main()
