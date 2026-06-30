# tests/test_td.py
import os, sys, time, shutil, tempfile, unittest
import subprocess as _sp
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import td
import tasks as TK
import status as S
import checkpoint as CP
import cycle


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


class PauseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        TK.create_task(self.tmp, "t1", repo="/r")  # state=queued

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_pause_sets_flag_and_dispatch_skips(self):
        td.cmd_pause(self.tmp, "t1")
        self.assertTrue(S.read(self.tmp, "t1").get("paused"))
        launched = []
        def fake_launcher(root, tid):
            launched.append(tid); return True
        cycle.dispatch_queued(self.tmp, fake_launcher, max_workers=4)
        self.assertNotIn("t1", launched)  # paused → not dispatched

    def test_resume_clears_flag_and_dispatch_proceeds(self):
        td.cmd_pause(self.tmp, "t1"); td.cmd_resume(self.tmp, "t1")
        self.assertFalse(S.read(self.tmp, "t1").get("paused"))
        launched = []
        cycle.dispatch_queued(self.tmp, lambda r, t: launched.append(t) or True, max_workers=4)
        self.assertIn("t1", launched)


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


class InterveneTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        TK.create_task(self.tmp, "t1", repo="/r")
        S.update(self.tmp, "t1", {"state": "running"})
    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _spawn_sleeper(self):
        p = _sp.Popen(["sleep", "30"])
        S.update(self.tmp, "t1", {"worker_pid": p.pid})
        return p

    def test_kill_worker_terminates_live_pid(self):
        p = self._spawn_sleeper()
        self.assertTrue(td._kill_worker(self.tmp, "t1"))
        p.wait(timeout=5)
        self.assertNotEqual(p.returncode, None)

    def test_kill_worker_false_when_no_pid(self):
        S.update(self.tmp, "t1", {"worker_pid": None})
        self.assertFalse(td._kill_worker(self.tmp, "t1"))

    def test_abort_requeue_resets_state_and_calls_reclaim(self):
        p = self._spawn_sleeper()
        calls = []
        td.cmd_abort(self.tmp, "t1", mode="requeue", runner=lambda c, **k: calls.append(c))
        p.wait(timeout=5)
        d = S.read(self.tmp, "t1")
        self.assertEqual(d["state"], "queued")
        self.assertIsNone(d["worker_pid"])
        self.assertTrue(any("reclaim-worktree.sh" in " ".join(c) for c in calls))

    def test_abort_fail_sets_failed_with_reason(self):
        self._spawn_sleeper()
        td.cmd_abort(self.tmp, "t1", mode="fail", runner=lambda c, **k: None)
        d = S.read(self.tmp, "t1")
        self.assertEqual(d["state"], "failed")
        self.assertTrue(d["failure_reason"])

    def test_redirect_steers_then_relaunches(self):
        self._spawn_sleeper()
        calls = []
        td.cmd_redirect(self.tmp, "t1", "go investigate O-proj", runner=lambda c, **k: calls.append(c))
        self.assertIn("go investigate O-proj", CP.read_new_steer(self.tmp, "t1"))
        self.assertTrue(any("launch-worker.sh" in " ".join(c) and "--resume" in c for c in calls))
