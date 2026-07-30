"""fork master ← upstream master 주기 sync (morning 루틴 단계).

모킹 대신 **실제 git 레포 3개**(bare origin=fork · bare upstream · 로컬 클론)로 검증한다.
네트워크 없이 로컬 경로 remote 로 실제 fetch/push 까지 일어나므로, "push 했다/안 했다" 를
bare 레포의 master sha 로 직접 확인할 수 있다.
"""
import os, shutil, subprocess, sys, tempfile, unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import morning as M

ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
           GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")


def _git(path, *args):
    return subprocess.run(["git", "-C", path, *args], check=True,
                          capture_output=True, text=True, env=ENV)


def _commit(repo, msg):
    with open(os.path.join(repo, "f"), "a") as f:
        f.write(msg + "\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)


def _sha(repo, ref="master"):
    return _git(repo, "rev-parse", "--short=7", ref).stdout.strip()


def make_fork(tmp, name, *, upstream_ahead=0, fork_only=0, upstream_remote=True):
    """실제 레포 3개를 만든다: bare upstream · bare fork(origin) · 로컬 클론.

    upstream_ahead: upstream master 가 fork 보다 앞선 커밋 수.
    fork_only: fork master 에만 있는 커밋 수(분기 시나리오).
    upstream_remote: False 면 로컬에 upstream remote 를 등록하지 않는다.
    반환 {"local", "up", "fork"} 경로.
    """
    up = os.path.join(tmp, name + "-up.git")
    fork = os.path.join(tmp, name + "-fork.git")
    seed = os.path.join(tmp, name + "-seed")
    for p, extra in ((up, ["--bare"]), (fork, ["--bare"]), (seed, [])):
        subprocess.run(["git", "init", "-q", *extra, "-b", "master", p], check=True, env=ENV)
    _commit(seed, "c1")
    _git(seed, "push", "-q", up, "master")
    _git(seed, "push", "-q", fork, "master")
    for i in range(upstream_ahead):
        _commit(seed, f"u{i}")
    if upstream_ahead:
        _git(seed, "push", "-q", up, "master")

    local = os.path.join(tmp, name)
    subprocess.run(["git", "clone", "-q", "-o", "origin", fork, local], check=True, env=ENV)
    if upstream_remote:
        _git(local, "remote", "add", "upstream", up)
    for i in range(fork_only):
        _commit(local, f"o{i}")
    if fork_only:
        _git(local, "push", "-q", "origin", "master")
    return {"local": local, "up": up, "fork": fork}


class ForkSyncRepoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_behind_fork_is_fast_forwarded(self):
        """①fork 가 뒤처짐 → 실제 ff push 가 일어나고 fork master 가 upstream 과 같아진다."""
        s = make_fork(self.tmp, "repo", upstream_ahead=2)
        before = _sha(s["fork"])
        rec = M.fork_sync_repo(s["local"])
        self.assertEqual(rec["status"], "synced")
        self.assertEqual(rec["branch"], "master")
        self.assertEqual(rec["behind"], 2)
        self.assertEqual(rec["old"], before)
        self.assertEqual(rec["new"], _sha(s["up"]))
        self.assertEqual(_sha(s["fork"]), _sha(s["up"]))  # 실제 push 됨

    def test_push_moves_local_origin_head_target(self):
        """ff push 는 로컬 refs/remotes/origin/master 도 옮긴다 — pool.default_ref() 의 base."""
        s = make_fork(self.tmp, "repo", upstream_ahead=2)
        M.fork_sync_repo(s["local"])
        self.assertEqual(_sha(s["local"], "refs/remotes/origin/master"), _sha(s["up"]))

    def test_already_current_is_noop(self):
        """②이미 최신 → push 없음(no-op)."""
        s = make_fork(self.tmp, "repo")
        before = _sha(s["fork"])
        rec = M.fork_sync_repo(s["local"])
        self.assertEqual(rec["status"], "up-to-date")
        self.assertEqual(_sha(s["fork"]), before)

    def test_diverged_fork_is_not_pushed(self):
        """③fork 가 분기됨 → push 하지 않고 분기 커밋 수를 보고한다(force 금지)."""
        s = make_fork(self.tmp, "repo", upstream_ahead=2, fork_only=1)
        before = _sha(s["fork"])
        rec = M.fork_sync_repo(s["local"])
        self.assertEqual(rec["status"], "diverged")
        self.assertEqual(rec["ahead"], 1)
        self.assertEqual(_sha(s["fork"]), before)          # fork 고유 커밋 보존
        self.assertNotEqual(_sha(s["fork"]), _sha(s["up"]))

    def test_missing_upstream_remote_is_skipped(self):
        """④upstream remote 없음 → skip(아무것도 건드리지 않음)."""
        s = make_fork(self.tmp, "repo", upstream_ahead=2, upstream_remote=False)
        before = _sha(s["fork"])
        rec = M.fork_sync_repo(s["local"])
        self.assertEqual(rec["status"], "skip")
        self.assertIn("upstream", rec["detail"])
        self.assertEqual(_sha(s["fork"]), before)

    def test_dry_run_reports_without_pushing(self):
        """dry_run=True → push 하지 않고 무엇을 할지만 보고."""
        s = make_fork(self.tmp, "repo", upstream_ahead=2)
        before = _sha(s["fork"])
        rec = M.fork_sync_repo(s["local"], dry_run=True)
        self.assertEqual(rec["status"], "synced")
        self.assertTrue(rec["dry_run"])
        self.assertEqual(rec["behind"], 2)
        self.assertEqual(_sha(s["fork"]), before)          # push 안 됨

    def test_undeterminable_default_branch_is_skipped(self):
        """default 브랜치를 못 얻으면 하드코딩하지 않고 skip + 이유 보고."""
        s = make_fork(self.tmp, "repo", upstream_ahead=2)
        _git(s["local"], "symbolic-ref", "-d", "refs/remotes/origin/HEAD")
        before = _sha(s["fork"])
        rec = M.fork_sync_repo(s["local"])
        self.assertEqual(rec["status"], "skip")
        self.assertIn("default", rec["detail"])
        self.assertEqual(_sha(s["fork"]), before)

    def test_default_branch_falls_back_to_upstream_head(self):
        """origin/HEAD 가 없어도 upstream/HEAD 가 있으면 그걸 쓴다."""
        s = make_fork(self.tmp, "repo", upstream_ahead=2)
        _git(s["local"], "symbolic-ref", "-d", "refs/remotes/origin/HEAD")
        _git(s["local"], "fetch", "-q", "upstream", "master")
        _git(s["local"], "remote", "set-head", "upstream", "master")
        self.assertEqual(M.default_branch(s["local"]), "master")

    def test_unreachable_upstream_is_error_not_raise(self):
        """네트워크/권한 실패는 예외가 아니라 error 레코드로 보고한다."""
        s = make_fork(self.tmp, "repo", upstream_ahead=2, upstream_remote=False)
        _git(s["local"], "remote", "add", "upstream", os.path.join(self.tmp, "nope.git"))
        rec = M.fork_sync_repo(s["local"])
        self.assertEqual(rec["status"], "error")
        self.assertTrue(rec["detail"])


class ForkSyncAllTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_syncs_repos_referenced_by_tasks(self):
        s = make_fork(self.tmp, "repo", upstream_ahead=2)
        res = M.fork_sync(self.tmp, [{"id": "t", "repo": s["local"]}])
        self.assertEqual([r["status"] for r in res], ["synced"])
        self.assertEqual(_sha(s["fork"]), _sha(s["up"]))

    def test_per_repo_failure_does_not_abort_others(self):
        """레포 A 가 터져도 레포 B 는 정상 sync 된다(pool_maintenance 패턴)."""
        a = make_fork(self.tmp, "repo_a", upstream_ahead=2)
        b = make_fork(self.tmp, "repo_b", upstream_ahead=2)
        a_abs = os.path.abspath(a["local"])
        real = M.fork_sync_repo

        def faulty(repo, **kw):
            if os.path.abspath(repo) == a_abs:
                raise RuntimeError("simulated broken repo")
            return real(repo, **kw)

        with patch.object(M, "fork_sync_repo", faulty):
            res = M.fork_sync(self.tmp, [{"id": "x", "repo": a["local"]},
                                         {"id": "y", "repo": b["local"]}])
        by = {os.path.basename(r["repo"]): r for r in res}
        self.assertEqual(by["repo_a"]["status"], "error")
        self.assertEqual(by["repo_b"]["status"], "synced")
        self.assertEqual(_sha(b["fork"]), _sha(b["up"]))


class ForkSyncDigestTest(unittest.TestCase):
    def test_digest_shows_synced_diverged_and_skip(self):
        fork_res = [
            {"repo": "/r/npu-tools", "status": "synced", "branch": "master",
             "old": "9ee5e88", "new": "5457b03", "behind": 2808, "dry_run": False},
            {"repo": "/r/other", "status": "diverged", "branch": "master", "ahead": 3},
            {"repo": "/r/tokendance", "status": "skip", "detail": "upstream remote 없음"},
            {"repo": "/r/cur", "status": "up-to-date", "branch": "master"},
        ]
        out = M.build_digest([], [], now_str="2026-07-30 07:00 KST", fork_res=fork_res)
        self.assertIn("🔄 fork sync", out)
        self.assertIn("npu-tools: 9ee5e88→5457b03 (2808커밋)", out)
        self.assertIn("분기됨 3커밋", out)
        self.assertIn("tokendance: skip(upstream remote 없음)", out)
        self.assertIn("cur: 최신", out)

    def test_digest_omits_fork_section_when_absent(self):
        self.assertNotIn("fork sync", M.build_digest([], [], now_str="x"))


class RunMorningWiringTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_run_morning_reports_fork_sync_in_digest(self):
        canned = [{"repo": "/r/npu-tools", "status": "synced", "branch": "master",
                   "old": "aaaaaaa", "new": "bbbbbbb", "behind": 7, "dry_run": False}]
        with patch.object(M, "fork_sync", lambda *a, **kw: canned) as _:
            res = M.run_morning(self.tmp, post=False)
        self.assertIn("npu-tools: aaaaaaa→bbbbbbb (7커밋)", res["digest"])
        self.assertEqual(res["fork"], canned)

    def test_run_morning_survives_fork_sync_failure(self):
        def boom(*a, **kw):
            raise RuntimeError("simulated fork sync failure")

        with patch.object(M, "fork_sync", boom):
            res = M.run_morning(self.tmp, post=False)
        self.assertTrue(res["digest"])
        self.assertIn("🌅", res["digest"])          # 다이제스트 정상 생성
        self.assertIn("🧹 풀 정리", res["digest"])   # pool GC 도 계속 진행됨

    def test_run_morning_dry_run_passes_dry_run_to_fork_sync(self):
        seen = {}

        def spy(root, tasks, **kw):
            seen.update(kw)
            return []

        with patch.object(M, "fork_sync", spy):
            M.run_morning(self.tmp, post=False, dry_run=True)
        self.assertTrue(seen.get("dry_run"))


if __name__ == "__main__":
    unittest.main()
