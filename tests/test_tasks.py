import os, sys, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import tasks as TK
import status as S


class TasksTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_create_scaffolds_files(self):
        td = TK.create_task(self.root, "t1", title="제목", repo="r")
        for name in ("status.json", "task.md", "progress.md",
                     "steer.md", "steer.cursor", "knowledge.md", "review.md"):
            self.assertTrue(os.path.exists(os.path.join(td, name)), name)
        with open(os.path.join(td, "steer.cursor")) as f:
            self.assertEqual(f.read().strip(), "0")

    def test_list_filters_by_state(self):
        TK.create_task(self.root, "t1")
        TK.create_task(self.root, "t2")
        S.update(self.root, "t2", {"state": "running", "worker_pid": 1})
        running = TK.list_tasks(self.root, state="running")
        self.assertEqual([d["id"] for d in running], ["t2"])
        self.assertEqual(len(TK.list_tasks(self.root)), 2)

    def test_count_running(self):
        TK.create_task(self.root, "t1")
        TK.create_task(self.root, "t2")
        S.update(self.root, "t1", {"state": "running", "worker_pid": 1})
        self.assertEqual(TK.count_running(self.root), 1)

    def test_archive_done_moves_dir(self):
        TK.create_task(self.root, "t1")
        S.update(self.root, "t1", {"state": "done"})
        dst = TK.archive(self.root, "t1")
        self.assertFalse(os.path.exists(os.path.join(self.root, "state", "tasks", "t1")))
        self.assertTrue(os.path.exists(os.path.join(dst, "status.json")))
        self.assertEqual(TK.list_tasks(self.root), [])          # 목록에서 사라짐

    def test_archive_refuses_active(self):
        TK.create_task(self.root, "t1")
        S.update(self.root, "t1", {"state": "running"})
        with self.assertRaises(ValueError):
            TK.archive(self.root, "t1")
        self.assertTrue(os.path.exists(os.path.join(self.root, "state", "tasks", "t1")))

    def test_archive_removes_clean_worktree(self):
        # done + worktree 가 깨끗(추적 변경 없음) → worktree 제거하고 archive 성공.
        TK.create_task(self.root, "t1")
        S.update(self.root, "t1", {"state": "done"})
        wt = os.path.join(self.root, "state", "worktrees", "t1")
        os.makedirs(wt)
        open(os.path.join(wt, "artifact.so"), "w").close()   # untracked 산출물 — 무시되고 버려짐
        TK.archive(self.root, "t1")
        self.assertFalse(os.path.isdir(wt))                  # worktree 제거됨
        self.assertTrue(os.path.exists(
            os.path.join(self.root, "state", "tasks-archive", "t1", "status.json")))

    def test_archive_refuses_worktree_with_tracked_changes(self):
        # worktree 가 진짜 git 레포이고 추적 파일에 미커밋 변경 → archive 거부(진짜 unsaved 보호).
        import subprocess
        TK.create_task(self.root, "t1")
        S.update(self.root, "t1", {"state": "done"})
        wt = os.path.join(self.root, "state", "worktrees", "t1")
        os.makedirs(wt)
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        subprocess.run(["git", "-C", wt, "init", "-q"], check=True)
        open(os.path.join(wt, "f.txt"), "w").write("a")
        subprocess.run(["git", "-C", wt, "add", "f.txt"], check=True)
        subprocess.run(["git", "-C", wt, "commit", "-qm", "init"], check=True, env=env)
        open(os.path.join(wt, "f.txt"), "w").write("b")      # 추적 파일 수정(미커밋)
        with self.assertRaises(ValueError):
            TK.archive(self.root, "t1")
        self.assertTrue(os.path.isdir(wt))                   # 보호: 안 지움
        # done 은 tasks-done/ 에 산다(분리). archive 거부됐으니 그 자리에 그대로 남아야 함.
        self.assertTrue(os.path.exists(os.path.join(self.root, "state", "tasks-done", "t1")))


if __name__ == "__main__":
    unittest.main()
