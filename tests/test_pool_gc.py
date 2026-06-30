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
