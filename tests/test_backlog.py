# tests/test_backlog.py — scripts/backlog.py 함수단위.
# NOTE: task.md 는 `unittest_` 접두를 적었으나, python3 -m unittest 는 `test` 접두만
# 수집한다(기존 tokendance Python 테스트 전부 `test_` 사용). 실제로 돌게 `test_` 접두를 쓴다.
import os, sys, shutil, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import backlog as BL
import status as S


class BacklogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = self.tmp

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ── add ──────────────────────────────────────────────────────────
    def test_add_creates_open_entry_with_tags(self):
        eid = BL.add(self.root, "fix rope kernel numerics", tags=["kernel", "perf"])
        e = BL.get(self.root, eid)
        self.assertEqual(e["id"], eid)
        self.assertEqual(e["text"], "fix rope kernel numerics")
        self.assertEqual(e["status"], "open")
        self.assertEqual(e["tags"], ["kernel", "perf"])
        self.assertTrue(e["created"])
        self.assertIsNone(e["promoted_task_id"])

    def test_add_persists_json_under_state_backlog(self):
        eid = BL.add(self.root, "idea")
        p = os.path.join(self.root, "state", "backlog", eid + ".json")
        self.assertTrue(os.path.exists(p))

    def test_add_without_tags_is_empty_list(self):
        eid = BL.add(self.root, "no tags")
        self.assertEqual(BL.get(self.root, eid)["tags"], [])

    def test_burst_add_no_collision(self):
        ids = [BL.add(self.root, "same text") for _ in range(5)]
        self.assertEqual(len(set(ids)), 5)          # 파일명 전부 distinct
        self.assertEqual(len(BL.ls(self.root)), 5)  # 5개 모두 디스크에

    # ── ls ───────────────────────────────────────────────────────────
    def test_ls_returns_all_sorted_by_id(self):
        a = BL.add(self.root, "first")
        b = BL.add(self.root, "second")
        ids = [e["id"] for e in BL.ls(self.root)]
        self.assertEqual(set(ids), {a, b})
        self.assertEqual(ids, sorted(ids))          # 계약: id(생성순) 오름차순

    def test_ls_filters_by_tag(self):
        a = BL.add(self.root, "a", tags=["x"])
        BL.add(self.root, "b", tags=["y"])
        self.assertEqual([e["id"] for e in BL.ls(self.root, tag="x")], [a])

    def test_ls_filters_by_status(self):
        a = BL.add(self.root, "a")
        b = BL.add(self.root, "b")
        BL.drop(self.root, b)
        self.assertEqual([e["id"] for e in BL.ls(self.root, status="open")], [a])
        self.assertEqual([e["id"] for e in BL.ls(self.root, status="dropped")], [b])

    def test_ls_combines_tag_and_status(self):
        a = BL.add(self.root, "a", tags=["x"])
        b = BL.add(self.root, "b", tags=["x"])
        BL.drop(self.root, b)
        self.assertEqual([e["id"] for e in BL.ls(self.root, tag="x", status="open")], [a])

    # ── get / show ───────────────────────────────────────────────────
    def test_get_missing_raises(self):
        with self.assertRaises(ValueError):
            BL.get(self.root, "does-not-exist")

    # ── tag ──────────────────────────────────────────────────────────
    def test_tag_adds_and_dedups_preserving_order(self):
        eid = BL.add(self.root, "a", tags=["x"])
        BL.tag(self.root, eid, ["y", "x"])          # x 는 이미 있음
        self.assertEqual(BL.get(self.root, eid)["tags"], ["x", "y"])

    def test_tag_remove(self):
        eid = BL.add(self.root, "a", tags=["x", "y"])
        BL.tag(self.root, eid, ["x"], remove=True)
        self.assertEqual(BL.get(self.root, eid)["tags"], ["y"])

    # ── drop ─────────────────────────────────────────────────────────
    def test_drop_sets_status(self):
        eid = BL.add(self.root, "a")
        BL.drop(self.root, eid)
        self.assertEqual(BL.get(self.root, eid)["status"], "dropped")

    # ── promote ──────────────────────────────────────────────────────
    def test_promote_creates_task_and_marks_entry(self):
        eid = BL.add(self.root, "make the widget", tags=["ui"])
        tid = BL.promote(self.root, eid, repo="/repos/x", task_id="t-promoted")
        self.assertEqual(tid, "t-promoted")
        d = S.read(self.root, "t-promoted")
        self.assertEqual(d["state"], "queued")
        self.assertEqual(d["repo"], os.path.abspath("/repos/x"))
        task_md = os.path.join(S.task_dir(self.root, "t-promoted"), "task.md")
        with open(task_md) as f:
            body = f.read()
        self.assertIn("make the widget", body)      # 원문 심김
        self.assertIn(eid, body)                     # 출처(backlog id) 참조
        e = BL.get(self.root, eid)
        self.assertEqual(e["status"], "promoted")
        self.assertEqual(e["promoted_task_id"], "t-promoted")

    def test_promote_generates_task_id_from_text_when_not_given(self):
        eid = BL.add(self.root, "auto id idea")
        tid = BL.promote(self.root, eid, repo="/r")
        self.assertTrue(tid)
        self.assertEqual(S.read(self.root, tid)["state"], "queued")
        self.assertIn("auto-id-idea", tid)           # slug from text

    def test_promote_refuses_already_promoted(self):
        eid = BL.add(self.root, "idea")
        BL.promote(self.root, eid, repo="/r", task_id="t1")
        with self.assertRaises(ValueError):
            BL.promote(self.root, eid, repo="/r", task_id="t2")

    # ── round-trip (acceptance) ──────────────────────────────────────
    def test_round_trip_add_ls_tag_show_promote_drop(self):
        eid = BL.add(self.root, "round trip idea", tags=["a"])
        self.assertIn(eid, [e["id"] for e in BL.ls(self.root, tag="a")])
        BL.tag(self.root, eid, ["b"])
        self.assertEqual(BL.get(self.root, eid)["tags"], ["a", "b"])
        tid = BL.promote(self.root, eid, repo="/r", task_id="rt-task")
        self.assertEqual(tid, "rt-task")
        self.assertEqual(BL.get(self.root, eid)["status"], "promoted")
        eid2 = BL.add(self.root, "drop me")
        BL.drop(self.root, eid2)
        self.assertEqual(BL.get(self.root, eid2)["status"], "dropped")


if __name__ == "__main__":
    unittest.main()
