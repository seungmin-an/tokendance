# tests/test_pool_gc.py
import os
import shutil
import tempfile
import time
import unittest

from scripts import pool


def _init_repo(path):
    import subprocess
    os.makedirs(path, exist_ok=True)
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    run = lambda *a: subprocess.run(["git", "-C", path, *a], check=True,
                                    capture_output=True, text=True, env=env)
    run("init", "-q", "-b", "main")
    open(os.path.join(path, "README"), "w").write("hi\n")
    open(os.path.join(path, ".gitignore"), "w").write("target/\n.venv\n")
    run("add", "-A"); run("commit", "-q", "-m", "init")
    return path


def _write(path, nbytes):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"x" * nbytes)


class DiskReportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = _init_repo(os.path.join(self.tmp, "repo"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_dir_size_sums_files_and_ignores_missing(self):
        self.assertEqual(pool.dir_size(os.path.join(self.tmp, "nope")), 0)
        _write(os.path.join(self.tmp, "d", "a.bin"), 1000)
        _write(os.path.join(self.tmp, "d", "sub", "b.bin"), 500)
        self.assertEqual(pool.dir_size(os.path.join(self.tmp, "d")), 1500)

    def test_disk_report_lists_slot_target_sizes_and_total(self):
        p = pool.acquire(self.repo, "task-1", root=self.tmp)
        _write(os.path.join(p, "target", "cache.bin"), 2048)
        rep = pool.disk_report(self.repo, root=self.tmp)
        self.assertEqual(rep["total_bytes"], 2048)
        self.assertEqual(len(rep["slots"]), 1)
        self.assertEqual(rep["slots"][0]["target_bytes"], 2048)
        self.assertTrue(rep["slots"][0]["leased"])


class GcTargetsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = _init_repo(os.path.join(self.tmp, "repo"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _cfg(self, **kv):
        with open(os.path.join(self.tmp, "config.local.md"), "w") as f:
            for k, v in kv.items():
                f.write(f"{k}={v}\n")

    def _idle_slot_with_target(self, holder, nbytes, age_days):
        p = pool.acquire(self.repo, holder, root=self.tmp)
        pool.release(self.repo, p, root=self.tmp)          # now idle
        _write(os.path.join(p, "target", "c.bin"), nbytes)
        old = time.time() - age_days * 86400
        os.utime(os.path.join(p, "target"), (old, old))
        return p

    def test_idle_sweep_evicts_old_target_only(self):
        self._cfg(POOL_TARGET_IDLE_DAYS=10, POOL_MAX_TREES=4)
        old = self._idle_slot_with_target("t-old", 1000, age_days=30)
        fresh = self._idle_slot_with_target("t-fresh", 1000, age_days=1)
        acts = pool.gc_targets(self.repo, root=self.tmp)
        names = {a["name"] for a in acts}
        # old slot's target evicted; fresh slot's target kept
        self.assertFalse(os.path.exists(os.path.join(old, "target")))
        self.assertTrue(os.path.exists(os.path.join(fresh, "target")))
        self.assertTrue(names)  # at least the old slot acted on

    def test_size_cap_evicts_coldest_until_under_lowwater(self):
        # cap 0.000003 GB ~ 3221 bytes; lowwater derived 0.8x ~ 2576
        self._cfg(POOL_TARGET_IDLE_DAYS=0, POOL_TARGET_MAX_GB=0.000003, POOL_MAX_TREES=4)
        a = self._idle_slot_with_target("a", 2000, age_days=5)   # coldest
        b = self._idle_slot_with_target("b", 2000, age_days=1)   # warmest
        pool.gc_targets(self.repo, root=self.tmp)
        # total was 4000 > cap; coldest (a) evicted first → under lowwater, b kept
        self.assertFalse(os.path.exists(os.path.join(a, "target")))
        self.assertTrue(os.path.exists(os.path.join(b, "target")))

    def test_never_evicts_leased_slot(self):
        self._cfg(POOL_TARGET_IDLE_DAYS=1)
        p = pool.acquire(self.repo, "live", root=self.tmp)       # leased
        _write(os.path.join(p, "target", "c.bin"), 1000)
        old = time.time() - 99 * 86400
        os.utime(os.path.join(p, "target"), (old, old))
        pool.gc_targets(self.repo, root=self.tmp)
        self.assertTrue(os.path.exists(os.path.join(p, "target")))  # leased → untouched

    def test_dry_run_frees_nothing(self):
        self._cfg(POOL_TARGET_IDLE_DAYS=1)
        p = self._idle_slot_with_target("t", 1000, age_days=9)
        acts = pool.gc_targets(self.repo, root=self.tmp, dry_run=True)
        self.assertTrue(os.path.exists(os.path.join(p, "target")))  # nothing removed
        self.assertTrue(acts)  # but the action is reported
