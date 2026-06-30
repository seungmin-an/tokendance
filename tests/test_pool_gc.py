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
    with open(os.path.join(path, "README"), "w") as f:
        f.write("hi\n")
    with open(os.path.join(path, ".gitignore"), "w") as f:
        f.write("target/\n.venv\n")
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

    def _idle_slots(self, specs):
        # specs: list of (holder, nbytes, age_days). Acquire all holders first so each
        # gets a DISTINCT slot (all leased simultaneously), then release + plant targets.
        paths = [pool.acquire(self.repo, h, root=self.tmp) for (h, _, _) in specs]
        for p, (_, nbytes, age) in zip(paths, specs):
            pool.release(self.repo, p, root=self.tmp)
            _write(os.path.join(p, "target", "c.bin"), nbytes)
            old = time.time() - age * 86400
            os.utime(os.path.join(p, "target"), (old, old))
        return paths

    def test_idle_sweep_evicts_old_target_only(self):
        self._cfg(POOL_TARGET_IDLE_DAYS=10, POOL_MAX_TREES=4)
        old, fresh = self._idle_slots([("t-old", 1000, 30), ("t-fresh", 1000, 1)])
        acts = pool.gc_targets(self.repo, root=self.tmp)
        names = {a["name"] for a in acts}
        # old slot's target evicted; fresh slot's target kept
        self.assertFalse(os.path.exists(os.path.join(old, "target")))
        self.assertTrue(os.path.exists(os.path.join(fresh, "target")))
        self.assertTrue(names)  # at least the old slot acted on

    def test_size_cap_evicts_coldest_until_under_lowwater(self):
        # cap 0.000003 GB ~ 3221 bytes; lowwater derived 0.8x ~ 2576
        self._cfg(POOL_TARGET_IDLE_DAYS=0, POOL_TARGET_MAX_GB=0.000003, POOL_MAX_TREES=4)
        a, b = self._idle_slots([("a", 2000, 5), ("b", 2000, 1)])
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
        p, = self._idle_slots([("t", 1000, 9)])
        acts = pool.gc_targets(self.repo, root=self.tmp, dry_run=True)
        self.assertTrue(os.path.exists(os.path.join(p, "target")))  # nothing removed
        self.assertTrue(acts)  # but the action is reported


class ReclaimStaleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = _init_repo(os.path.join(self.tmp, "repo"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _leased(self):
        return {e["lease_holder"] for e in
                pool.load_state(pool.pool_dir(self.repo, self.tmp))["entries"] if e["leased"]}

    def test_reclaims_only_holders_not_in_keep_set(self):
        pool.acquire(self.repo, "live", root=self.tmp)
        pool.acquire(self.repo, "dead", root=self.tmp)
        reclaimed = pool.reclaim_stale(self.repo, root=self.tmp, keep_holders={"live"})
        self.assertEqual(set(reclaimed), {"dead"})
        self.assertEqual(self._leased(), {"live"})

    def test_keep_all_reclaims_nothing(self):
        pool.acquire(self.repo, "a", root=self.tmp)
        pool.acquire(self.repo, "b", root=self.tmp)
        self.assertEqual(pool.reclaim_stale(self.repo, root=self.tmp,
                                            keep_holders={"a", "b"}), [])
        self.assertEqual(self._leased(), {"a", "b"})

    def test_expected_holder_mismatch_leaves_slot_leased(self):
        """TOCTOU guard: release with wrong expected_holder must NOT clear the lease."""
        path = pool.acquire(self.repo, "x", root=self.tmp)
        # Simulate a stale snapshot: caller thinks holder was "someone-else", but slot
        # is actually held by "x". release must skip (not clear).
        pool.release(self.repo, path, self.tmp, expected_holder="someone-else")
        self.assertEqual(self._leased(), {"x"})  # still leased to "x"
        # Correct expected_holder clears it normally.
        pool.release(self.repo, path, self.tmp, expected_holder="x")
        self.assertEqual(self._leased(), set())  # now released
