import os, sys, json, unittest, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import harvest_knowledge as HK
import status as S


def _make_task(root, tid, log, repo=""):
    S.init(root, tid, title=tid, repo=repo)
    td = os.path.join(root, "state", "tasks", tid)
    with open(os.path.join(td, "knowledge.md"), "w") as f:
        f.write(log)
    return td


class ParseTest(unittest.TestCase):
    def test_extracts_title_and_body(self):
        log = (
            "# log\n\n잡담 줄.\n\n"
            "## 지식: flock 으로 동시성 직렬화\n"
            "fcntl.flock(LOCK_EX) 로 status.json 쓰기를 직렬화한다.\n\n"
            "## 다른 섹션\n무시됨.\n"
        )
        blocks = HK.parse_knowledge_blocks(log)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["title"], "flock 으로 동시성 직렬화")
        self.assertIn("LOCK_EX", blocks[0]["body"])
        self.assertNotIn("다른 섹션", blocks[0]["body"])
        self.assertNotIn("무시됨", blocks[0]["body"])

    def test_block_at_eof(self):
        log = "## 지식: 끝까지\nbody line\n"
        blocks = HK.parse_knowledge_blocks(log)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["body"].strip(), "body line")

    def test_multiple_blocks(self):
        log = (
            "## 지식: 첫째\nA\n\n"
            "## 지식: 둘째\nB\n"
        )
        blocks = HK.parse_knowledge_blocks(log)
        self.assertEqual([b["title"] for b in blocks], ["첫째", "둘째"])

    def test_parses_meta_lines(self):
        log = (
            "## 지식: 레포 빌드법\n"
            "scope: repo\n"
            "repo: tokendance\n"
            "tags: build, ci\n"
            "\n"
            "cargo build --release 로 빌드한다.\n"
        )
        b = HK.parse_knowledge_blocks(log)[0]
        self.assertEqual(b["meta"]["scope"], "repo")
        self.assertEqual(b["meta"]["repo"], "tokendance")
        self.assertEqual(b["meta"]["tags"], "build, ci")
        self.assertEqual(b["body"].strip(), "cargo build --release 로 빌드한다.")

    def test_no_meta_means_body_starts_immediately(self):
        log = "## 지식: 메타없음\n바로 본문 시작.\n"
        b = HK.parse_knowledge_blocks(log)[0]
        self.assertEqual(b["meta"], {})
        self.assertEqual(b["body"].strip(), "바로 본문 시작.")


class HarvestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        os.makedirs(os.path.join(self.root, "library"))
        with open(os.path.join(self.root, "library", "index.md"), "w") as f:
            f.write("# 목차\n")

    def tearDown(self):
        self.tmp.cleanup()

    def _index(self):
        with open(os.path.join(self.root, "library", "index.md")) as f:
            return f.read()

    def _lib(self, *parts):
        return os.path.join(self.root, "library", *parts)

    def test_playbook_entry_creates_file_and_index_link(self):
        _make_task(self.root, "t1",
                   "## 지식: 재시도 패턴\nscope: playbook\nsummary: 3회 재시도\n\n"
                   "지수 백오프로 3회 재시도한다.\n")
        summary = HK.harvest(self.root)
        self.assertIn("playbook:재시도-패턴", summary["created"])
        path = self._lib("playbooks", "재시도-패턴.md")
        self.assertTrue(os.path.exists(path), path)
        with open(path) as f:
            content = f.read()
        self.assertIn("지수 백오프", content)
        # 인덱스에 상대 링크가 있고 깨지지 않는다.
        self.assertIn("playbooks/재시도-패턴.md", self._index())

    def test_repo_scope_groups_into_single_repo_file(self):
        _make_task(self.root, "t1",
                   "## 지식: 빌드법\nrepo: alpha\n\ncargo build.\n")
        _make_task(self.root, "t2",
                   "## 지식: 테스트법\nrepo: alpha\n\ncargo test.\n")
        HK.harvest(self.root)
        path = self._lib("repos", "alpha.md")
        self.assertTrue(os.path.exists(path))
        with open(path) as f:
            content = f.read()
        self.assertIn("## 빌드법", content)
        self.assertIn("## 테스트법", content)
        self.assertIn("cargo build", content)
        self.assertIn("cargo test", content)

    def test_repo_falls_back_to_task_repo_field(self):
        # scope=repo 인데 meta repo 미지정 → task status.json 의 repo 사용.
        _make_task(self.root, "t1",
                   "## 지식: 비밀\nscope: repo\n\n레포 특정 지식.\n",
                   repo="beta")
        HK.harvest(self.root)
        self.assertTrue(os.path.exists(self._lib("repos", "beta.md")))

    def test_default_scope_is_playbook(self):
        _make_task(self.root, "t1", "## 지식: 범용\n\n그냥 노하우.\n", repo="gamma")
        HK.harvest(self.root)
        self.assertTrue(os.path.exists(self._lib("playbooks", "범용.md")))
        self.assertFalse(os.path.exists(self._lib("repos", "gamma.md")))

    def test_idempotent_rerun_makes_no_changes(self):
        _make_task(self.root, "t1", "## 지식: 안정\n\n바뀌지 않는 본문.\n")
        HK.harvest(self.root)
        first = self._index()
        summary2 = HK.harvest(self.root)
        self.assertEqual(summary2["created"], [])
        self.assertEqual(summary2["updated"], [])
        self.assertEqual(summary2["skipped"], ["playbook:안정"])
        self.assertEqual(self._index(), first)

    def test_changed_body_updates_entry(self):
        td = _make_task(self.root, "t1", "## 지식: 진화\n\n옛 본문.\n")
        HK.harvest(self.root)
        with open(os.path.join(td, "knowledge.md"), "w") as f:
            f.write("## 지식: 진화\n\n새 본문 v2.\n")
        summary = HK.harvest(self.root)
        self.assertEqual(summary["updated"], ["playbook:진화"])
        with open(self._lib("playbooks", "진화.md")) as f:
            content = f.read()
        self.assertIn("새 본문 v2", content)
        self.assertNotIn("옛 본문", content)

    def test_index_has_both_sections(self):
        _make_task(self.root, "t1", "## 지식: 노하우\n\nA\n")
        _make_task(self.root, "t2", "## 지식: 레포지식\nrepo: alpha\n\nB\n")
        HK.harvest(self.root)
        idx = self._index()
        self.assertIn("playbooks/노하우.md", idx)
        self.assertIn("repos/alpha.md", idx)

    def test_ledger_persisted(self):
        _make_task(self.root, "t1", "## 지식: 기록\n\n본문.\n")
        HK.harvest(self.root)
        ledger_path = self._lib(".harvest-ledger.json")
        self.assertTrue(os.path.exists(ledger_path))
        with open(ledger_path) as f:
            data = json.load(f)
        self.assertIn("playbook:기록", data["entries"])
        self.assertEqual(data["entries"]["playbook:기록"]["sources"], ["t1"])


class TierRenderTest(unittest.TestCase):
    """tier-aware 렌더: candidate 는 1급(playbooks/repos/index)에서 제외, candidates.md 로 격리.
    tier 누락(레거시) 엔트리는 1급으로 렌더(하위호환)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        os.makedirs(os.path.join(self.root, "library"))

    def tearDown(self):
        self.tmp.cleanup()

    def _ledger(self, entries):
        return {"version": 1, "entries": entries}

    def test_candidate_excluded_from_primary_present_in_candidates(self):
        ledger = self._ledger({
            "playbook:일급": {"title": "일급", "scope": "playbook", "repo": None,
                             "slug": "일급", "dest": "playbooks/일급.md", "anchor": None,
                             "summary": "", "tags": "", "body": "확실한 본문",
                             "tier": "primary", "sources": ["t1"]},
            "candidate:playbook:후보": {"title": "후보", "scope": "playbook", "repo": None,
                                       "slug": "후보", "dest": "playbooks/후보.md",
                                       "anchor": None, "summary": "추정", "tags": "",
                                       "body": "불확실 본문", "tier": "candidate",
                                       "sources": ["t2"]},
        })
        HK._render_library(self.root, ledger)
        lib = os.path.join(self.root, "library")
        self.assertTrue(os.path.exists(os.path.join(lib, "playbooks", "일급.md")))
        self.assertFalse(os.path.exists(os.path.join(lib, "playbooks", "후보.md")))
        with open(os.path.join(lib, "candidates.md")) as f:
            cand = f.read()
        self.assertIn("후보", cand)
        self.assertIn("불확실 본문", cand)
        self.assertNotIn("확실한 본문", cand)
        with open(os.path.join(lib, "index.md")) as f:
            idx = f.read()
        self.assertIn("일급", idx)
        self.assertIn("candidates.md", idx)   # candidate 섹션 링크

    def test_legacy_entry_without_tier_renders_as_primary(self):
        ledger = self._ledger({
            "playbook:레거시": {"title": "레거시", "scope": "playbook", "repo": None,
                               "slug": "레거시", "dest": "playbooks/레거시.md",
                               "anchor": None, "summary": "", "tags": "",
                               "body": "tier 없음", "sources": ["t1"]},  # tier 필드 없음
        })
        HK._render_library(self.root, ledger)
        self.assertTrue(os.path.exists(
            os.path.join(self.root, "library", "playbooks", "레거시.md")))


class HarvestLockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        os.makedirs(os.path.join(self.root, "library"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_harvest_creates_lock_file_and_succeeds(self):
        _make_task(self.root, "t1", "## 지식: 락\n\n본문.\n")
        HK.harvest(self.root)
        self.assertTrue(os.path.exists(HK._lock_path(self.root)))
        self.assertIn("playbook:락", HK.load_ledger(self.root)["entries"])


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        os.makedirs(os.path.join(self.root, "library"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_main_runs_and_reports_counts(self):
        import io
        from contextlib import redirect_stdout
        _make_task(self.root, "t1", "## 지식: CLI\n\n본문.\n")
        buf = io.StringIO()
        with redirect_stdout(buf):
            HK.main(["--root", self.root])
        out = buf.getvalue()
        self.assertIn("created=1", out)
        self.assertTrue(os.path.exists(
            os.path.join(self.root, "library", "playbooks", "cli.md")))


if __name__ == "__main__":
    unittest.main()
