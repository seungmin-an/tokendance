import os, sys, shutil, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import harvest_knowledge as HK

def _entry(title, scope, repo, slug, summary="", tags="", tier="primary"):
    dest = f"repos/{HK.slugify(repo)}.md" if scope == "repo" else f"playbooks/{slug}.md"
    return {"title": title, "scope": scope, "repo": repo, "slug": slug, "dest": dest,
            "anchor": HK.anchor(title) if scope == "repo" else None,
            "summary": summary, "tags": tags, "body": "b", "tier": tier, "sources": ["t1"]}

class RecallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _seed(self, *entries):
        HK.save_ledger(self.tmp, {"version": 1,
            "entries": {f"{e['scope']}:{e.get('repo')}:{e['slug']}": e for e in entries}})

    def test_empty_ledger_returns_empty(self):
        self.assertEqual(HK.recall_block(self.tmp, "/repos/foo"), "")

    def test_selects_playbook_and_matching_repo_excludes_others(self):
        self._seed(
            _entry("pb one", "playbook", None, "pb-one", summary="generic", tags="build"),
            _entry("foo fact", "repo", "foo", "foo-fact", summary="about foo"),
            _entry("bar fact", "repo", "bar", "bar-fact", summary="about bar"),
            _entry("cand", "playbook", None, "cand", summary="unsure", tier="candidate"),
        )
        out = HK.recall_block(self.tmp, "/some/path/foo")   # matches repo "foo" by basename
        self.assertIn("pb one", out)         # playbook included
        self.assertIn("foo fact", out)       # matching repo included
        self.assertIn("about foo", out)      # summary shown
        self.assertIn("library/repos/foo.md", out)  # pointer shown
        self.assertNotIn("bar fact", out)    # other repo excluded
        self.assertNotIn("cand", out)        # candidate excluded

    def test_repo_match_is_by_basename(self):
        self._seed(_entry("foo fact", "repo", "/abs/path/foo", "foo-fact"))
        self.assertIn("foo fact", HK.recall_block(self.tmp, "/root/foo"))  # basename foo == foo

    def test_caps_entries_and_prioritizes_repo_over_playbook(self):
        with open(os.path.join(self.tmp, "config.local.md"), "w") as f:
            f.write("RECALL_MAX_ENTRIES=2\n")
        self._seed(
            _entry("foo fact 1", "repo", "foo", "foo-fact-1"),
            _entry("foo fact 2", "repo", "foo", "foo-fact-2"),
            _entry("foo fact 3", "repo", "foo", "foo-fact-3"),
            _entry("pb one", "playbook", None, "pb-one"),
        )
        out = HK.recall_block(self.tmp, "/some/path/foo")
        entry_lines = [ln for ln in out.splitlines() if ln.startswith("- ")]
        self.assertLessEqual(len(entry_lines), 2)          # capped at RECALL_MAX_ENTRIES
        self.assertIn("foo fact 1", out)                   # repo-scoped kept preferentially
        self.assertIn("foo fact 2", out)
        self.assertNotIn("foo fact 3", out)                # excess repo entry dropped
        self.assertNotIn("pb one", out)                     # playbook dropped entirely (cap exhausted by repo)
        self.assertIn("더", out)                            # truncation note present
        self.assertIn("(+2개 더", out)                      # 1 dropped repo entry + 1 dropped playbook
