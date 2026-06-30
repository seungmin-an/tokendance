import os, sys, time, shutil, tempfile, unittest
from unittest.mock import patch
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import morning as M
import pool


def _init_repo(path):
    import subprocess
    os.makedirs(path, exist_ok=True)
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    run = lambda *a: subprocess.run(["git", "-C", path, *a], check=True,
                                    capture_output=True, text=True, env=env)
    run("init", "-q", "-b", "main")
    with open(os.path.join(path, ".gitignore"), "w") as f:
        f.write("target/\n.venv\n")
    with open(os.path.join(path, "README"), "w") as f:
        f.write("hi\n")
    run("add", "-A"); run("commit", "-q", "-m", "init")
    return path


class PoolMaintenanceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = _init_repo(os.path.join(self.tmp, "repo"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_live_holders_keeps_fresh_heartbeat_drops_stale(self):
        now = time.time()
        fresh = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 60))
        stale = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 100 * 3600))
        tasks = [
            {"id": "a", "repo": self.repo, "heartbeat": fresh},
            {"id": "b", "repo": self.repo, "heartbeat": stale},
            {"id": "c", "repo": self.repo, "heartbeat": None},
        ]
        keep = M.live_holders(tasks, now, ttl_hours=48)
        self.assertIn("a", keep[self.repo])
        self.assertNotIn("b", keep[self.repo])
        self.assertNotIn("c", keep[self.repo])

    def test_pool_maintenance_reclaims_stale_lease(self):
        pool.acquire(self.repo, "a", root=self.tmp)   # leased, will be "dead"
        with open(os.path.join(self.tmp, "config.local.md"), "w") as f:
            f.write("POOL_LEASE_TTL_HOURS=48\nPOOL_TARGET_IDLE_DAYS=0\n")
        # task 'a' absent from the task list → holder gone → reclaimed
        res = M.pool_maintenance(self.tmp, [], now=time.time(),
                                 log=lambda m: None, dry_run=False)
        self.assertIn("a", res["reclaimed"])
        leased = {e["lease_holder"] for e in
                  pool.load_state(pool.pool_dir(self.repo, self.tmp))["entries"] if e["leased"]}
        self.assertEqual(leased, set())

    def test_pool_maintenance_resilient_bad_repo(self):
        """C1 resilience: bad repo A does not abort maintenance; good repo B is reclaimed."""
        repo_a = _init_repo(os.path.join(self.tmp, "repo_a"))
        repo_b = _init_repo(os.path.join(self.tmp, "repo_b"))
        # Acquire stale leases in both repos.
        pool.acquire(repo_a, "holder_a", root=self.tmp)
        pool.acquire(repo_b, "holder_b", root=self.tmp)
        with open(os.path.join(self.tmp, "config.local.md"), "w") as f:
            f.write("POOL_LEASE_TTL_HOURS=48\nPOOL_TARGET_IDLE_DAYS=0\n")
        # Patch reclaim_stale so it raises for repo_a but works normally for repo_b.
        repo_a_abs = os.path.abspath(repo_a)
        _real_reclaim = pool.reclaim_stale
        def _faulty_reclaim(repo, root=None, keep_holders=None):
            if os.path.abspath(repo) == repo_a_abs:
                raise RuntimeError("simulated corrupt repo")
            return _real_reclaim(repo, root=root, keep_holders=keep_holders or set())
        with patch.object(pool, "reclaim_stale", _faulty_reclaim):
            # Inject both repos via tasks so pool_maintenance includes repo_a.
            tasks = [{"id": "x", "repo": repo_a}, {"id": "y", "repo": repo_b}]
            # Should NOT raise even though repo A is broken.
            res = M.pool_maintenance(self.tmp, tasks, now=time.time(),
                                     log=lambda m: None, dry_run=False)
        # repo B's stale lease must be reclaimed.
        self.assertIn("holder_b", res["reclaimed"])
        # repo A must appear in failed list.
        failed_basenames = [os.path.basename(r) for r in res["failed"]]
        self.assertIn("repo_a", failed_basenames)

    def test_run_morning_guard_pool_failure(self):
        """C1 run_morning guard: digest is still produced even when pool_maintenance raises."""
        def _failing_pool(*args, **kwargs):
            raise RuntimeError("simulated pool failure")

        with patch.object(M, "pool_maintenance", _failing_pool):
            result = M.run_morning(self.tmp, post=False)
        self.assertIn("digest", result)
        self.assertTrue(result["digest"])

    def test_build_digest_includes_reclaimed_ids(self):
        """I1 digest: reclaimed holder ids appear in build_digest output."""
        pool_res = {
            "reclaimed": ["t1"],
            "target_actions": [],
            "disk": {},
            "failed": [],
        }
        out = M.build_digest([], [], now_str="2026-06-30 07:00 KST", pool_res=pool_res)
        self.assertIn("t1", out)
