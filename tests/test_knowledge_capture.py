import os, sys, shutil, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import harvest_knowledge as HK
import tasks as TK
import status as S


class KnowledgeCaptureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        TK.create_task(self.tmp, "t1", repo="/repos/foo")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_scaffold_creates_knowledge_md_not_log_md(self):
        d = S.task_dir(self.tmp, "t1")
        self.assertTrue(os.path.exists(os.path.join(d, "knowledge.md")))
        self.assertFalse(os.path.exists(os.path.join(d, "log.md")))

    def test_harvest_reads_knowledge_md(self):
        d = S.task_dir(self.tmp, "t1")
        with open(os.path.join(d, "knowledge.md"), "w") as f:
            f.write("## 지식: use -p from root\nscope: playbook\n\nrun cargo test -p x\n")
        HK.harvest(self.tmp)
        entries = HK.load_ledger(self.tmp)["entries"]
        self.assertTrue(any("use -p from root" == e["title"] for e in entries.values()))


if __name__ == "__main__":
    unittest.main()
