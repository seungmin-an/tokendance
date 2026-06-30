# tests/test_pool.py
import json
import os
import tempfile
import unittest

from scripts import pool


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


if __name__ == "__main__":
    unittest.main()
