# tests/test_pool.py
import json
import os
import subprocess
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


if __name__ == "__main__":
    unittest.main()
