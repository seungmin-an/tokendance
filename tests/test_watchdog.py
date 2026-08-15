import os, sys, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import watchdog as WD


class WatchdogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        os.makedirs(os.path.join(self.root, "state"))
        self.started = []

    def tearDown(self):
        self.tmp.cleanup()

    def _pidfile(self, content):
        with open(os.path.join(self.root, "state", "supervisor.pid"), "w") as f:
            f.write(content)

    def _stopmarker(self):
        open(os.path.join(self.root, "state", "supervisor.stopped"), "w").close()

    def _start_ok(self):
        self.started.append(True)
        return True

    def _start_fail(self):
        self.started.append(True)
        return False

    # ── supervisor_down: 프로세스 생존이 유일한 판정 기준 ──

    def test_down_when_pidfile_missing(self):
        self.assertTrue(WD.supervisor_down(self.root))

    def test_down_when_pid_not_alive(self):
        self._pidfile("999999")     # 존재할 수 없는 pid
        self.assertTrue(WD.supervisor_down(self.root))

    def test_down_when_pidfile_garbage(self):
        self._pidfile("nonsense")
        self.assertTrue(WD.supervisor_down(self.root))

    def test_up_when_pid_alive(self):
        self._pidfile(str(os.getpid()))
        self.assertFalse(WD.supervisor_down(self.root))

    # ── check_once ──

    def test_restarts_when_down(self):
        action = WD.check_once(self.root, start=self._start_ok)
        self.assertEqual(action, "restarted")
        self.assertEqual(len(self.started), 1)

    def test_noop_when_up(self):
        self._pidfile(str(os.getpid()))
        action = WD.check_once(self.root, start=self._start_ok)
        self.assertEqual(action, "ok")
        self.assertEqual(self.started, [])

    def test_stop_marker_blocks_restart(self):
        """stop.sh 로 의도적으로 내린 상태는 워치독이 되살리지 않는다."""
        self._stopmarker()
        action = WD.check_once(self.root, start=self._start_ok)
        self.assertEqual(action, "stopped")
        self.assertEqual(self.started, [])

    def test_reports_start_failure(self):
        action = WD.check_once(self.root, start=self._start_fail)
        self.assertEqual(action, "restart-failed")

    def test_restart_is_logged(self):
        WD.check_once(self.root, start=self._start_ok)
        with open(os.path.join(self.root, "state", "watchdog.log")) as f:
            self.assertIn("restarted", f.read())

    # ── 알림은 엣지 트리거(같은 상태가 이어지면 재알림 없음) ──

    def test_notify_on_new_action_only(self):
        self.assertTrue(WD.should_notify("ok", "restarted"))
        self.assertFalse(WD.should_notify("restarted", "restarted"))
        self.assertFalse(WD.should_notify("ok", "ok"))
        self.assertFalse(WD.should_notify("restarted", "ok"))
        self.assertFalse(WD.should_notify("ok", "stopped"))
        self.assertTrue(WD.should_notify("restarted", "restart-failed"))


if __name__ == "__main__":
    unittest.main()
