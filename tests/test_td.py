# tests/test_td.py
import os, sys, time, shutil, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import td
import tasks as TK
import status as S
import checkpoint as CP


class ReadCommandsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_status_lists_tasks_with_state(self):
        TK.create_task(self.tmp, "t1", title="A", repo="/r")
        S.update(self.tmp, "t1", {"state": "running"})
        rows = td.cmd_status(self.tmp)
        ids = {r[0]: r for r in rows}
        self.assertIn("t1", ids)
        self.assertEqual(ids["t1"][1], "running")

    def test_peek_shows_progress_and_pending_steer_without_consuming(self):
        TK.create_task(self.tmp, "t1", repo="/r")
        td_dir = S.task_dir(self.tmp, "t1")
        with open(os.path.join(td_dir, "progress.md"), "w") as f:
            f.write("working on it")
        with open(os.path.join(td_dir, "steer.md"), "w") as f:
            f.write("do X instead")
        out = td.cmd_peek(self.tmp, "t1")
        self.assertIn("working on it", out)
        self.assertIn("do X instead", out)
        # cursor not advanced — still 0
        with open(os.path.join(td_dir, "steer.cursor")) as f:
            self.assertEqual(f.read().strip(), "0")

    def test_logs_prints_worker_log(self):
        TK.create_task(self.tmp, "t1", repo="/r")
        logp = os.path.join(self.tmp, "state", "workers", "t1.log")
        os.makedirs(os.path.dirname(logp), exist_ok=True)
        with open(logp, "w") as f:
            f.write("line1\nline2\n")
        # cmd_logs writes to stdout; capture
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            td.cmd_logs(self.tmp, "t1", follow=False)
        self.assertIn("line2", buf.getvalue())


class SteerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(); TK.create_task(self.tmp, "t1", repo="/r")
    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_steer_append_is_seen_by_checkpoint(self):
        td.cmd_steer(self.tmp, "t1", "use bf16 not fp8")
        # the worker's checkpoint consumes new steer past the cursor
        seen = CP.read_new_steer(self.tmp, "t1")
        self.assertIn("use bf16 not fp8", seen)
        # second checkpoint sees nothing new (cursor advanced)
        self.assertEqual(CP.read_new_steer(self.tmp, "t1").strip(), "")
