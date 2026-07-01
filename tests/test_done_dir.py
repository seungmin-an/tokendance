"""done 디렉토리 분리: done → state/tasks-done/ 이동 + 양쪽조회 resolver + 멱등 마이그레이션.

active(state/tasks/) · done(state/tasks-done/) · archive(state/tasks-archive/) 3분리.
자동 archive 금지(수동 tasks.py archive 만). morning-GC 는 done 의 worktree 만 회수.
"""
import os, sys, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import status as S
import tasks as TK
import harvest_knowledge as HK
import morning as M


def _tasks(root):
    return os.path.join(root, "state", "tasks")


def _done(root):
    return os.path.join(root, "state", "tasks-done")


class DoneDirTransitionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_done_transition_moves_dir_to_tasks_done(self):
        TK.create_task(self.root, "t1")
        S.update(self.root, "t1", {"state": "done"})
        self.assertFalse(os.path.exists(os.path.join(_tasks(self.root), "t1")))
        self.assertTrue(os.path.exists(os.path.join(_done(self.root), "t1", "status.json")))

    def test_resolver_reads_done_task_from_new_location(self):
        TK.create_task(self.root, "t1")
        S.update(self.root, "t1", {"state": "done"})
        # read/task_dir 가 tasks-done/ 에서 찾아야 한다.
        self.assertEqual(S.read(self.root, "t1")["state"], "done")
        self.assertEqual(S.task_dir(self.root, "t1"),
                         os.path.join(_done(self.root), "t1"))

    def test_active_task_stays_in_tasks(self):
        TK.create_task(self.root, "t1")
        S.update(self.root, "t1", {"state": "running"})
        self.assertEqual(S.task_dir(self.root, "t1"),
                         os.path.join(_tasks(self.root), "t1"))
        self.assertFalse(os.path.isdir(os.path.join(_done(self.root), "t1")))

    def test_failed_stays_in_tasks(self):
        # failed 는 done 이 아니므로 active 에 남는다(조사/복구 여지).
        TK.create_task(self.root, "t1")
        S.update(self.root, "t1", {"state": "failed", "failure_reason": "x"})
        self.assertTrue(os.path.isdir(os.path.join(_tasks(self.root), "t1")))
        self.assertFalse(os.path.isdir(os.path.join(_done(self.root), "t1")))

    def test_done_then_heartbeat_idempotent(self):
        TK.create_task(self.root, "t1")
        S.update(self.root, "t1", {"state": "done"})
        S.heartbeat(self.root, "t1")            # done 상태 추가 update — 위치 불변
        self.assertTrue(os.path.isdir(os.path.join(_done(self.root), "t1")))
        self.assertFalse(os.path.isdir(os.path.join(_tasks(self.root), "t1")))

    def test_done_rollback_moves_back_to_tasks(self):
        # done → 다른상태 되돌림(드묾): active 로 복귀.
        TK.create_task(self.root, "t1")
        S.update(self.root, "t1", {"state": "done"})
        S.update(self.root, "t1", {"state": "running"})
        self.assertTrue(os.path.isdir(os.path.join(_tasks(self.root), "t1")))
        self.assertFalse(os.path.isdir(os.path.join(_done(self.root), "t1")))


class ListAcrossBasesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_list_tasks_sees_done_in_new_location(self):
        TK.create_task(self.root, "a")
        TK.create_task(self.root, "b")
        S.update(self.root, "b", {"state": "done"})
        ids = sorted(d["id"] for d in TK.list_tasks(self.root))
        self.assertEqual(ids, ["a", "b"])
        done = [d["id"] for d in TK.list_tasks(self.root, state="done")]
        self.assertEqual(done, ["b"])

    def test_all_task_ids_unique_sorted(self):
        TK.create_task(self.root, "a")
        TK.create_task(self.root, "b")
        S.update(self.root, "a", {"state": "done"})
        self.assertEqual(S.all_task_ids(self.root), ["a", "b"])


class ArchiveFromDoneTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_archive_done_from_tasks_done_dir(self):
        TK.create_task(self.root, "t1")
        S.update(self.root, "t1", {"state": "done"})    # → tasks-done/
        dst = TK.archive(self.root, "t1")
        self.assertTrue(os.path.exists(os.path.join(dst, "status.json")))
        self.assertFalse(os.path.isdir(os.path.join(_done(self.root), "t1")))
        self.assertFalse(os.path.isdir(os.path.join(_tasks(self.root), "t1")))
        self.assertEqual(TK.list_tasks(self.root), [])

    def test_archive_failed_from_tasks_dir(self):
        TK.create_task(self.root, "t1")
        S.update(self.root, "t1", {"state": "failed", "failure_reason": "x"})
        dst = TK.archive(self.root, "t1")
        self.assertTrue(os.path.exists(os.path.join(dst, "status.json")))


class MigrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def _seed_legacy_done(self, tid):
        """마이그레이션 전 상태 모사: done 인데 아직 state/tasks/ 에 있는 dir."""
        TK.create_task(self.root, tid)
        # status.json 만 done 으로 직접 기록(이동 로직 우회)해 legacy 배치 재현.
        import json
        sp = os.path.join(_tasks(self.root), tid, "status.json")
        with open(sp) as f:
            d = json.load(f)
        d["state"] = "done"
        with open(sp, "w") as f:
            json.dump(d, f)

    def test_migrate_moves_legacy_done(self):
        self._seed_legacy_done("old")
        TK.create_task(self.root, "active")
        moved = TK.migrate_done(self.root)
        self.assertEqual(moved, ["old"])
        self.assertTrue(os.path.isdir(os.path.join(_done(self.root), "old")))
        self.assertTrue(os.path.isdir(os.path.join(_tasks(self.root), "active")))

    def test_migrate_idempotent(self):
        self._seed_legacy_done("old")
        TK.migrate_done(self.root)
        self.assertEqual(TK.migrate_done(self.root), [])     # 재실행 무해
        self.assertTrue(os.path.isdir(os.path.join(_done(self.root), "old")))


class HarvestAcrossBasesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_harvest_iterates_done_logs(self):
        TK.create_task(self.root, "t1")
        with open(os.path.join(_tasks(self.root), "t1", "knowledge.md"), "w") as f:
            f.write("hello")
        S.update(self.root, "t1", {"state": "done"})    # → tasks-done/
        seen = {tid for tid, repo, text in HK._iter_task_logs(self.root)}
        self.assertIn("t1", seen)


class MorningDoneTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_gc_reclaims_done_worktree_in_new_location(self):
        TK.create_task(self.root, "t1")
        # prepare-worktree writes worktree.path into the ACTIVE task dir at dispatch
        wt = os.path.join(self.root, "state", "pool", "repo", "1")
        os.makedirs(wt)
        with open(os.path.join(_tasks(self.root), "t1", "worktree.path"), "w") as f:
            f.write(wt)
        S.update(self.root, "t1", {"state": "done"})  # relocates task dir (+worktree.path) to tasks-done/
        calls = []

        def runner(cmd):
            calls.append(cmd)
            import subprocess
            return subprocess.CompletedProcess(cmd, 0, "", "")

        res = M.run_morning(self.root, post=False, runner=runner)
        actions = {a["task"]: a for a in res["gc"]}
        self.assertIn("t1", actions)
        self.assertNotEqual(actions["t1"]["action"], "skip")  # slot_path resolves via tasks-done/

    def test_morning_never_archives_done(self):
        TK.create_task(self.root, "t1")
        S.update(self.root, "t1", {"state": "done"})
        M.run_morning(self.root, post=False)
        # done 은 tasks-done/ 에 그대로(archive 로 자동 이동 금지).
        self.assertTrue(os.path.isdir(os.path.join(_done(self.root), "t1")))
        self.assertFalse(os.path.isdir(os.path.join(self.root, "state", "tasks-archive", "t1")))


if __name__ == "__main__":
    unittest.main()
