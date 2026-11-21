"""Plano de versão: análise que não viu o último commit, issue sem tipo único e mudança revertida."""

import unittest

import support  # noqa: F401  (põe scripts/ no sys.path antes de importar devflow)
from devflow.plan import validate_analysis
from devflow.release import analysis_comment
from support import BASE_CONFIG, SandboxCase

FEAT = [("feat: add the export command", None)]
FIX = [("fix: handle the empty list", None)]
API = [
    ("fix: require the page size", {"api/client.py": "def fetch(size):\n    return size\n"}),
    ("fix: require the page size and the order", {"api/client.py": "def fetch(size, order):\n    return size\n"}),
]


def _analysis(head, *entries):
    changes = ", ".join(
        f'{{"pr": {number}, "level": "{level}", "summary": "{summary}", "evidence": []}}'
        for number, level, summary in entries
    )
    return f'<!-- dev-workflow:analysis\n{{"schema": 1, "base": "v1.2.0", "head": "{head}", "changes": [{changes}]}}\n-->\nanálise'


class StaleAnalysisTest(SandboxCase):
    config = BASE_CONFIG.replace("[versioning]", "[versioning]\nrequire_analysis = true")

    def test_an_analysis_followed_by_a_new_commit_does_not_count(self):
        box = self.box
        box.merge_pr(10, "fix: require the page size", API, labels=["release"])
        earlier = box.git("rev-parse", f"{box.head_of(10)}~1")  # a ponta do PR quando a análise foi feita
        box.comment(10, _analysis(earlier, (10, "patch", "o argumento novo tem valor padrão")))
        refused = box.release()
        self.assertEqual(refused.returncode, 1, refused.stdout)
        self.assertIn("análise anterior ao último commit", refused.stdout)
        self.assertIn("sem análise registrada: #10", refused.stdout)
        self.assertEqual(box.remote_tags(), ["v1.2.0"])

        box.comment(10, _analysis(box.head_of(10), (10, "patch", "o argumento novo tem valor padrão")))
        self.assertRan(box.release())
        self.assertEqual(box.remote_tags(), ["v1.2.0", "v1.2.1"])

    def test_an_analysis_without_head_does_not_count(self):
        box = self.box
        box.merge_pr(10, "fix: require the page size", API, labels=["release"])
        box.comment(10, _analysis("", (10, "patch", "o argumento novo tem valor padrão")))
        self.assertEqual(box.release().returncode, 1)

    def test_an_analysis_on_a_later_pr_still_covers_the_older_merged_ones(self):
        box = self.box
        box.merge_pr(10, "fix: require the page size", API)
        box.merge_pr(11, "fix: handle the empty list", FIX, labels=["release"])
        box.comment(
            11,
            _analysis(box.head_of(11), (10, "minor", "fetch ganhou argumentos com padrão"), (11, "patch", "correção")),
        )
        out = self.assertRan(box.release())
        self.assertIn("fetch ganhou argumentos com padrão → minor", out)
        self.assertEqual(box.remote_tags(), ["v1.2.0", "v1.3.0"])


class AnalysisCommentTest(unittest.TestCase):
    def test_a_summary_with_line_breaks_stays_one_table_row(self):
        data = validate_analysis(
            {
                "schema": 1,
                "head": "abc1234",
                "changes": [{"pr": 3, "level": "minor", "summary": "linha um\nlinha | dois", "evidence": ["a\nb"]}],
            }
        )
        rows = [line for line in analysis_comment(data).splitlines() if line.startswith("| #3 ")]
        self.assertEqual(len(rows), 1)
        self.assertIn("linha um linha / dois", rows[0])
        self.assertTrue(rows[0].endswith(" |"))


class UntypedIssueTest(SandboxCase):
    def test_an_issue_without_a_single_type_is_a_note_in_the_plan(self):
        box = self.box
        box.issue(1, "Exportar em CSV", "feature", "bug")
        box.issue(2, "Sem tipo")
        box.merge_pr(10, "feat: add the export command", FEAT, closes=[1, 2])
        out = self.assertRan(box.run("release.py", "plan"))
        self.assertIn("#1 (citada por #10) não tem exatamente um label de tipo (`bug`, `feature`)", out)
        self.assertIn("#2 (citada por #10) não tem exatamente um label de tipo (nenhum)", out)


class RevertedChangeTest(SandboxCase):
    def _revert_on_main(self, sha):
        box = self.box
        box.git("revert", "-m", "1", "--no-edit", sha)
        box.git("push", "-q", "origin", "main")
        return box.git("rev-parse", "HEAD")

    def test_a_feature_and_its_revert_both_pending_cancel_out(self):
        box = self.box
        pull = box.merge_pr(10, "feat: add the export command", FEAT)
        self._revert_on_main(pull["merge_commit_sha"])
        box.merge_pr(11, "fix: handle the empty list", FIX, labels=["release"])
        out = self.assertRan(box.release())
        self.assertEqual(box.remote_tags(), ["v1.2.0", "v1.2.1"])
        self.assertIn("reverte #10 (feat: add the export command) antes da release", out)
        changelog = box.read("CHANGELOG.md")
        self.assertNotIn("export", changelog)
        self.assertNotIn("Revert", changelog)
        self.assertIn("handle the empty list", changelog)

    def test_reverting_what_was_already_released_is_a_change(self):
        box = self.box
        pull = box.merge_pr(10, "feat: add the export command", FEAT, labels=["release"])
        self.assertRan(box.release())
        box.git("pull", "-q", "origin", "main")
        self._revert_on_main(pull["merge_commit_sha"])
        box.merge_pr(11, "fix: handle the empty list", FIX, labels=["release"])
        out = self.assertRan(box.release())
        self.assertNotIn("antes da release", out)
        self.assertIn("Revert", box.read("CHANGELOG.md").split("## [1.3.0]")[0])


if __name__ == "__main__":
    unittest.main()
