# tests/test_td.py
import os, re, sys, time, types, shutil, tempfile, unittest
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


class AttachTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        TK.create_task(self.tmp, "t1", repo="/r")
        S.update(self.tmp, "t1", {"state": "running", "worker_session_id": "SID-123"})
        self.wt = os.path.join(self.tmp, "wt")           # a real dir to attach into
        os.makedirs(self.wt)
        with open(os.path.join(S.task_dir(self.tmp, "t1"), "worktree.path"), "w") as f:
            f.write(self.wt + "\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _spy_execer(self):
        calls = []
        def execer(claude, argv, cwd, env):
            calls.append({"claude": claude, "argv": argv, "cwd": cwd, "env": env})
        return calls, execer

    def test_attach_errors_when_no_session(self):
        S.update(self.tmp, "t1", {"worker_session_id": None})
        calls, execer = self._spy_execer()
        with self.assertRaises(SystemExit) as ctx:
            td.cmd_attach(self.tmp, "t1", claude_bin="/fake/claude", execer=execer)
        self.assertNotEqual(ctx.exception.code, 0)   # non-zero exit
        self.assertEqual(calls, [])                  # never hands over

    def test_attach_errors_when_no_worktree(self):
        os.remove(os.path.join(S.task_dir(self.tmp, "t1"), "worktree.path"))
        calls, execer = self._spy_execer()
        with self.assertRaises(SystemExit):
            td.cmd_attach(self.tmp, "t1", claude_bin="/fake/claude", execer=execer)
        self.assertEqual(calls, [])

    def test_attach_live_worker_pauses_kills_then_resumes(self):
        pid = _spawn_orphan(["sleep", "30"])
        S.update(self.tmp, "t1", {"worker_pid": pid})
        self.addCleanup(lambda: (os.kill(pid, 9) if td._alive(pid) else None))
        calls, execer = self._spy_execer()
        td.cmd_attach(self.tmp, "t1", claude_bin="/fake/claude", execer=execer)
        self.assertFalse(td._alive(pid))             # worker stopped before handover
        d = S.read(self.tmp, "t1")
        self.assertEqual(d["state"], "queued")       # out of running (no stale-relaunch)
        self.assertTrue(d["paused"])                 # dispatch blocked during handover
        self.assertIsNone(d["worker_pid"])           # dead pid cleared
        self.assertEqual(d["worker_session_id"], "SID-123")  # session preserved for resume
        self.assertEqual(len(calls), 1)
        c = calls[0]
        self.assertEqual(c["claude"], "/fake/claude")
        self.assertEqual(c["cwd"], self.wt)          # cwd = worktree
        self.assertEqual(c["argv"][:3], ["/fake/claude", "--resume", "SID-123"])
        self.assertEqual(c["env"]["IS_SANDBOX"], "1")
        self.assertNotIn("--dangerously-skip-permissions", c["argv"])  # default keeps prompts

    def test_attach_dead_worker_skips_kill_and_resumes(self):
        pid = _spawn_orphan(["sleep", "30"])
        os.kill(pid, 9)
        td._wait_dead(pid, 2.0)
        self.assertFalse(td._alive(pid))
        S.update(self.tmp, "t1", {"worker_pid": pid})
        killed = []
        orig = td._kill_worker
        td._kill_worker = lambda *a, **k: killed.append(True)
        self.addCleanup(lambda: setattr(td, "_kill_worker", orig))
        calls, execer = self._spy_execer()
        td.cmd_attach(self.tmp, "t1", claude_bin="/fake/claude", execer=execer)
        self.assertEqual(killed, [])                 # dead worker → kill skipped
        self.assertEqual(len(calls), 1)              # went straight to resume
        d = S.read(self.tmp, "t1")
        self.assertEqual(d["state"], "queued")
        self.assertTrue(d["paused"])
        self.assertIsNone(d["worker_pid"])

    def test_attach_skip_permissions_adds_flag(self):
        S.update(self.tmp, "t1", {"worker_pid": None})   # no live worker
        calls, execer = self._spy_execer()
        td.cmd_attach(self.tmp, "t1", skip_permissions=True,
                      claude_bin="/fake/claude", execer=execer)
        self.assertIn("--dangerously-skip-permissions", calls[0]["argv"])


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


class OpenTest(unittest.TestCase):
    """`td task open` — provision a human-driven worktree session (attach's mirror)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.wt = os.path.join(self.tmp, "wt")   # a real dir prepare-worktree "provisions"
        os.makedirs(self.wt)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _runner(self, tid):
        """Spy runner: records argv, and for prepare-worktree simulates the real
        side effect (writes worktree.path — the source of truth cmd_open reads)."""
        calls = []

        def runner(cmd, **k):
            calls.append(cmd)
            if any("prepare-worktree.sh" in str(c) for c in cmd):
                td_dir = S.task_dir(self.tmp, tid)
                os.makedirs(td_dir, exist_ok=True)
                with open(os.path.join(td_dir, "worktree.path"), "w") as f:
                    f.write(self.wt + "\n")
            return types.SimpleNamespace(returncode=0, stdout=self.wt + "\n", stderr="")

        return calls, runner

    def _tmux_call(self, calls):
        return [c for c in calls if any("new-session" in str(x) for x in c)][0]

    def test_open_creates_task_worktree_session_queued_paused(self):
        calls, runner = self._runner("t-open")
        td.cmd_open(self.tmp, "/repos/x", "drive it", task_id="t-open",
                    claude_bin="/fake/claude", runner=runner,
                    which=lambda _: "/fake/tmux", recall_fn=lambda *a: "")
        d = S.read(self.tmp, "t-open")
        self.assertEqual(d["state"], "queued")           # not running → no stale-relaunch
        self.assertTrue(d["paused"])                     # dispatch blocked while human drives
        self.assertIsNone(d["worker_pid"])               # no headless worker
        self.assertTrue(d["worker_session_id"])          # session minted + recorded
        self.assertEqual(d["repo"], os.path.abspath("/repos/x"))
        # worktree provisioned via prepare-worktree, path recorded (source of truth)
        self.assertTrue(any("prepare-worktree.sh" in " ".join(c) for c in calls))
        self.assertEqual(td._worktree_path(self.tmp, "t-open"), self.wt)

    def test_open_tmux_argv_has_detached_session_cwd_claude_recall(self):
        calls, runner = self._runner("t-open")
        td.cmd_open(self.tmp, "/r", "d", task_id="t-open", claude_bin="/fake/claude",
                    runner=runner, which=lambda _: "/fake/tmux",
                    recall_fn=lambda *a: "RECALL-BLOB")
        sid = S.read(self.tmp, "t-open")["worker_session_id"]
        tmux = self._tmux_call(calls)
        self.assertEqual(tmux[0], "/fake/tmux")
        self.assertIn("new-session", tmux)
        self.assertIn("-d", tmux)                        # detached
        self.assertIn("td-t-open", tmux)                 # session name = td-<id>
        self.assertEqual(tmux[tmux.index("-c") + 1], self.wt)   # window cwd = worktree
        shell_cmd = tmux[-1]
        self.assertIn("IS_SANDBOX=1", shell_cmd)         # root boot
        self.assertIn("/fake/claude", shell_cmd)
        self.assertIn("--session-id", shell_cmd)
        self.assertIn(sid, shell_cmd)                    # the recorded session id is what claude gets
        self.assertIn("RECALL-BLOB", shell_cmd)          # library recall injected
        self.assertNotIn("--dangerously-skip-permissions", shell_cmd)  # default keeps prompts

    def test_open_skip_permissions_adds_flag(self):
        calls, runner = self._runner("t-open")
        td.cmd_open(self.tmp, "/r", "d", task_id="t-open", skip_permissions=True,
                    claude_bin="/fake/claude", runner=runner,
                    which=lambda _: "/fake/tmux", recall_fn=lambda *a: "")
        self.assertIn("--dangerously-skip-permissions", self._tmux_call(calls)[-1])

    def test_open_empty_recall_omits_append_system_prompt(self):
        calls, runner = self._runner("t-open")
        td.cmd_open(self.tmp, "/r", "d", task_id="t-open", claude_bin="/fake/claude",
                    runner=runner, which=lambda _: "/fake/tmux", recall_fn=lambda *a: "")
        self.assertNotIn("--append-system-prompt", self._tmux_call(calls)[-1])

    def test_open_errors_when_tmux_missing_and_provisions_nothing(self):
        calls = []
        with self.assertRaises(SystemExit) as ctx:
            td.cmd_open(self.tmp, "/r", "d", task_id="t-open", claude_bin="/fake/claude",
                        runner=lambda c, **k: calls.append(c),
                        which=lambda _: None, recall_fn=lambda *a: "")
        self.assertNotEqual(ctx.exception.code, 0)
        self.assertEqual(calls, [])                      # no subprocess ran
        with self.assertRaises(FileNotFoundError):       # errored before creating the task
            S.read(self.tmp, "t-open")

    def test_open_errors_when_claude_unset(self):
        old = os.environ.pop("TOKENDANCE_CLAUDE", None)
        self.addCleanup(lambda: os.environ.__setitem__("TOKENDANCE_CLAUDE", old)
                        if old is not None else None)
        with self.assertRaises(SystemExit):
            td.cmd_open(self.tmp, "/r", "d", task_id="t-open", claude_bin=None,
                        which=lambda _: "/fake/tmux", recall_fn=lambda *a: "")

    def test_open_errors_when_worktree_provisioning_fails(self):
        def runner(cmd, **k):
            if any("prepare-worktree.sh" in str(c) for c in cmd):
                return types.SimpleNamespace(returncode=1, stdout="", stderr="boom")
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        with self.assertRaises(SystemExit) as ctx:
            td.cmd_open(self.tmp, "/r", "d", task_id="t-open", claude_bin="/fake/claude",
                        runner=runner, which=lambda _: "/fake/tmux", recall_fn=lambda *a: "")
        self.assertNotEqual(ctx.exception.code, 0)

    def test_help_tree_includes_open(self):
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            td.main(["help"])
        self.assertIn("open", buf.getvalue())


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
        self.assertIn("task", out)
        self.assertIn("spawn", out)

    def test_bare_td_prints_tree_not_argparse_dump(self):
        out = self._run([])
        self.assertIn("├─", out)
        self.assertIn("└─", out)
        self.assertNotIn("usage:", out)

    def test_help_prints_tree_not_argparse_dump(self):
        out = self._run(["help"])
        self.assertIn("├─", out)
        self.assertIn("└─", out)
        self.assertNotIn("usage:", out)

    def test_help_lists_groups_and_subcommands(self):
        out = self._run(["help"])
        for c in ("task", "worktree", "help",
                  "ls", "peek", "steer", "spawn", "disk", "gc"):
            self.assertIn(c, out)

    def test_help_topic_shows_command_detail(self):
        out = self._run(["help", "task"])
        self.assertIn("usage: td task", out)
        self.assertIn("spawn", out)


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


class TaskNamespaceTest(unittest.TestCase):
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

    def test_task_ls_lists_tasks(self):
        out = self._run(["task", "ls"])
        self.assertIn("t1", out)

    def test_task_spawn_creates_queued_task(self):
        out = self._run(["task", "spawn", "--repo", self.repo, "desc"])
        self.assertTrue(out.strip())

    def test_top_level_status_is_no_longer_a_valid_command(self):
        with self.assertRaises(SystemExit) as ctx:
            td.main(["status"])
        self.assertEqual(ctx.exception.code, 2)

    def test_help_shows_nested_actions(self):
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            td.main(["help"])
        out = buf.getvalue()
        for c in ("task", "worktree", "peek", "steer", "spawn", "disk", "gc"):
            self.assertIn(c, out)


class BacklogCmdTest(unittest.TestCase):
    """`td backlog` — idea backlog group over scripts/backlog.py."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, argv):
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            td.main(["--root", self.tmp, *argv])  # must NOT raise
        return buf.getvalue()

    def test_add_prints_id_and_ls_shows_entry(self):
        eid = self._run(["backlog", "add", "cache the compile graph", "--tag", "perf"]).strip()
        self.assertTrue(eid)
        out = self._run(["backlog", "ls"])
        self.assertIn(eid, out)
        self.assertIn("cache the compile graph", out)
        self.assertIn("perf", out)

    def test_ls_status_filter(self):
        eid = self._run(["backlog", "add", "x"]).strip()
        self._run(["backlog", "drop", eid])
        self.assertIn(eid, self._run(["backlog", "ls", "--status", "dropped"]))
        self.assertNotIn(eid, self._run(["backlog", "ls", "--status", "open"]))

    def test_show_displays_text(self):
        eid = self._run(["backlog", "add", "showable idea"]).strip()
        self.assertIn("showable idea", self._run(["backlog", "show", eid]))

    def test_tag_add_and_remove(self):
        import backlog as BL
        eid = self._run(["backlog", "add", "y"]).strip()
        self._run(["backlog", "tag", eid, "alpha", "beta"])
        self.assertEqual(BL.get(self.tmp, eid)["tags"], ["alpha", "beta"])
        self._run(["backlog", "tag", eid, "alpha", "--remove"])
        self.assertEqual(BL.get(self.tmp, eid)["tags"], ["beta"])

    def test_promote_creates_task_and_marks_entry(self):
        import backlog as BL
        eid = self._run(["backlog", "add", "promote me", "--tag", "z"]).strip()
        tid = self._run(["backlog", "promote", eid, "--repo", "/repos/x", "--id", "bp-task"]).strip()
        self.assertEqual(tid, "bp-task")
        self.assertEqual(S.read(self.tmp, "bp-task")["state"], "queued")
        e = BL.get(self.tmp, eid)
        self.assertEqual(e["status"], "promoted")
        self.assertEqual(e["promoted_task_id"], "bp-task")

    def test_round_trip_via_main(self):
        import backlog as BL
        eid = self._run(["backlog", "add", "rt idea", "--tag", "a"]).strip()
        self.assertIn(eid, self._run(["backlog", "ls", "--tag", "a"]))
        self._run(["backlog", "tag", eid, "b"])
        self.assertIn("rt idea", self._run(["backlog", "show", eid]))
        tid = self._run(["backlog", "promote", eid, "--repo", "/r", "--id", "rt"]).strip()
        self.assertEqual(tid, "rt")
        self.assertEqual(BL.get(self.tmp, eid)["status"], "promoted")

    def test_help_tree_includes_backlog_group_and_subcommands(self):
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            td.main(["help"])
        out = buf.getvalue()
        self.assertIn("backlog", out)
        for c in ("promote", "drop"):        # subcommands unique to backlog
            self.assertIn(c, out)
