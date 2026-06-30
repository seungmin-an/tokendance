import os, sys, time, shutil, tempfile, unittest
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
