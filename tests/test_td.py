# tests/test_td.py
import os, re, sys, time, shutil, tempfile, unittest
import subprocess as _sp
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import td
import tasks as TK
import status as S
import checkpoint as CP
import cycle
import pool


def _init_repo(path):
    os.makedirs(path, exist_ok=True)
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    run = lambda *a: _sp.run(["git", "-C", path, *a], check=True,
                             capture_output=True, text=True, env=env)
    run("init", "-q", "-b", "main")
    with open(os.path.join(path, ".gitignore"), "w") as f:
        f.write("target/\n.venv\n")
    with open(os.path.join(path, "README"), "w") as f:
        f.write("hi\n")
    run("add", "-A"); run("commit", "-q", "-m", "init")
    return path


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


def _spawn_orphan(cmd):
    """Double-fork so the new process is NOT a child of this process (adopted by
    init/PID-1). This is necessary because os.kill(pid, 0) keeps returning True
    for zombie children even after they die — orphans disappear from the process
    table immediately, matching the production scenario where td never owns the
    worker process."""
    r, w = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(r)
        grandchild = os.fork()
        if grandchild == 0:
            os.close(w)
            os.execvp(cmd[0], cmd)
            os._exit(1)
        else:
            os.write(w, f"{grandchild}\n".encode())
            os.close(w)
            os._exit(0)
    os.close(w)
    data = b""
    while True:
        chunk = os.read(r, 64)
        if not chunk:
            break
        data += chunk
    os.close(r)
    os.waitpid(child, 0)
    return int(data.strip())


class InterveneTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        TK.create_task(self.tmp, "t1", repo="/r")
        S.update(self.tmp, "t1", {"state": "running"})
    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _spawn_sleeper(self):
        pid = _spawn_orphan(["sleep", "30"])
        S.update(self.tmp, "t1", {"worker_pid": pid})
        self.addCleanup(lambda: (
            (lambda: (os.kill(pid, 9) if td._alive(pid) else None))(),
        ))
        return pid

    def test_kill_worker_terminates_live_pid(self):
        pid = self._spawn_sleeper()
        self.assertTrue(td._kill_worker(self.tmp, "t1"))
        self.assertFalse(td._alive(pid))

    def test_kill_worker_escalates_to_sigkill(self):
        # spawn a process that ignores SIGTERM — _kill_worker must escalate to SIGKILL
        pid = _spawn_orphan(["bash", "-c", "trap '' TERM; sleep 30"])
        S.update(self.tmp, "t1", {"worker_pid": pid})
        self.addCleanup(lambda: (
            (lambda: (os.kill(pid, 9) if td._alive(pid) else None))(),
        ))
        result = td._kill_worker(self.tmp, "t1", timeout=2.0)
        self.assertTrue(result)
        self.assertFalse(td._alive(pid))

    def test_kill_worker_false_when_no_pid(self):
        S.update(self.tmp, "t1", {"worker_pid": None})
        self.assertFalse(td._kill_worker(self.tmp, "t1"))

    def test_abort_requeue_resets_state_and_calls_reclaim(self):
        self._spawn_sleeper()
        calls = []
        td.cmd_abort(self.tmp, "t1", mode="requeue", runner=lambda c, **k: calls.append(c))
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


class SpawnTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_spawn_creates_queued_task_with_repo_and_desc(self):
        tid = td.cmd_spawn(self.tmp, "/repos/x", "fix the rope kernel", task_id="t-fixed")
        self.assertEqual(tid, "t-fixed")
        d = S.read(self.tmp, "t-fixed")
        self.assertEqual(d["state"], "queued")
        self.assertEqual(d["repo"], os.path.abspath("/repos/x"))
        task_md = os.path.join(S.task_dir(self.tmp, "t-fixed"), "task.md")
        with open(task_md) as f:
            self.assertIn("fix the rope kernel", f.read())

    def test_spawn_generates_id_when_not_given(self):
        tid = td.cmd_spawn(self.tmp, "/r", "do a thing")
        self.assertTrue(tid.endswith("-do-a-thing") or "do-a-thing" in tid)
        self.assertEqual(S.read(self.tmp, tid)["state"], "queued")


class DiskGcTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = _init_repo(os.path.join(self.tmp, "repo"))
        TK.create_task(self.tmp, "t1", repo=self.repo)
    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_disk_reports_repo_total(self):
        p = pool.acquire(self.repo, "t1", root=self.tmp)
        os.makedirs(os.path.join(p, "target"), exist_ok=True)
        with open(os.path.join(p, "target", "c.bin"), "wb") as f:
            f.write(b"x" * 4096)
        rep = td.cmd_disk(self.tmp)
        repos = {r[0]: r for r in rep}
        self.assertIn(os.path.abspath(self.repo), repos)
        self.assertEqual(repos[os.path.abspath(self.repo)][1], 4096)

    def test_gc_dry_run_reports_without_freeing(self):
        with open(os.path.join(self.tmp, "config.local.md"), "w") as f:
            f.write("POOL_TARGET_IDLE_DAYS=1\n")
        p = pool.acquire(self.repo, "t1", root=self.tmp)
        pool.release(self.repo, p, root=self.tmp)
        os.makedirs(os.path.join(p, "target"), exist_ok=True)
        with open(os.path.join(p, "target", "c.bin"), "wb") as f:
            f.write(b"x" * 100)
        old = time.time() - 9 * 86400
        os.utime(os.path.join(p, "target"), (old, old))
        acts = td.cmd_gc(self.tmp, dry_run=True)
        self.assertTrue(acts)  # reported
        self.assertTrue(os.path.exists(os.path.join(p, "target")))  # not freed


class WorktreeLsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = _init_repo(os.path.join(self.tmp, "repo"))
        TK.create_task(self.tmp, "t1", repo=self.repo)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_ls_shows_slot_holder_state_and_target_size(self):
        p1 = pool.acquire(self.repo, "t1", root=self.tmp)
        pool.acquire(self.repo, "t2", root=self.tmp)
        os.makedirs(os.path.join(p1, "target"), exist_ok=True)
        with open(os.path.join(p1, "target", "x.bin"), "wb") as f:
            f.write(b"x" * 4096)

        rows = td.cmd_worktree_ls(self.tmp)

        self.assertEqual(len(rows), 2)
        by_holder = {r["holder"]: r for r in rows}
        self.assertIn("t1", by_holder)
        self.assertEqual(by_holder["t1"]["state"], "leased")
        self.assertGreaterEqual(by_holder["t1"]["target_bytes"], 4096)


class HelpTest(unittest.TestCase):
    def _run(self, argv):
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            td.main(argv)          # must NOT raise SystemExit
        return buf.getvalue()

    def test_bare_td_prints_help_without_error(self):
        out = self._run([])
        self.assertIn("status", out)
        self.assertIn("spawn", out)

    def test_help_lists_subcommands(self):
        out = self._run(["help"])
        for c in ("status", "peek", "steer", "spawn", "worktree"):
            self.assertIn(c, out)

    def test_help_topic_shows_command_detail(self):
        out = self._run(["help", "spawn"])
        self.assertIn("--repo", out)

    def test_top_level_help_has_no_standalone_disk_or_gc(self):
        out = self._run(["help"])
        self.assertIn("worktree", out)
        top_level_cmds = re.findall(r"^  (\S+)", out, re.MULTILINE)
        self.assertNotIn("disk", top_level_cmds)
        self.assertNotIn("gc", top_level_cmds)


class WorktreeDispatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = _init_repo(os.path.join(self.tmp, "repo"))
        TK.create_task(self.tmp, "t1", repo=self.repo)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, argv):
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            td.main(["--root", self.tmp, *argv])  # must NOT raise
        return buf.getvalue()

    def test_worktree_disk_dispatch_runs_without_error(self):
        pool.acquire(self.repo, "t1", root=self.tmp)
        out = self._run(["worktree", "disk"])
        self.assertIn("MiB", out)

    def test_worktree_gc_dry_run_dispatch_runs_without_error(self):
        out = self._run(["worktree", "gc", "--dry-run"])
        self.assertIn("FREED", out)

    def test_top_level_disk_is_no_longer_a_valid_command(self):
        with self.assertRaises(SystemExit) as ctx:
            td.main(["disk"])
        self.assertEqual(ctx.exception.code, 2)
