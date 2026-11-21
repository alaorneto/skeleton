"""Lacunas da issue #34 nos caminhos que decidem a versão e o changelog, de ponta a ponta.

Mesmo sandbox de ``test_e2e_release``: git de verdade com remoto *bare* e ``gh`` simulado, que
agora corta as listas em páginas de no máximo 100 itens, como a API.
"""

import json
import unittest

import support  # noqa: F401  (põe scripts/ no sys.path antes de importar devflow)
from support import BASE_CONFIG, SandboxCase

FIX = [("fix: handle the empty list", None)]
FEAT = [("feat: add the export command", None)]
MARK = "dev-workflow:released v1.2.1"


def _bodies(state, number):
    return [comment["body"] for comment in state["comments"].get(str(number), [])]


def _marks(state, number, mark=MARK):
    return [body for body in _bodies(state, number) if mark in body]


class RebaseMergeTest(SandboxCase):
    def test_the_commits_of_a_rebased_pr_are_one_change_of_that_pr(self):
        box = self.box
        box.issue(1, "Exportar em CSV", "feature")
        commits = [("feat: add the export command", None), ("fix: escape the separator", None)]
        pull = box.merge_pr(10, "feat: add the export command", commits, labels=["release"], closes=[1], rebase=True)
        self.assertEqual(box.log("-2"), ["fix: escape the separator", "feat: add the export command"])
        self.assertEqual(box.git("rev-list", "--merges", "v1.2.0..HEAD"), "")  # nenhum commit de merge
        self.assertEqual(len(pull["commit_shas"]), 2)

        plan = json.loads(self.assertRan(box.run("release.py", "plan", "--format", "json")))
        self.assertEqual([change["key"] for change in plan["changes"]], ["#10"])
        self.assertEqual(len(plan["changes"][0]["commits"]), 2)
        self.assertEqual(plan["requested_by"], ["#10"])

        self.assertRan(box.release())
        self.assertEqual(box.tags(), ["v1.2.0", "v1.3.0"])
        changelog = box.read("CHANGELOG.md")
        self.assertIn("Exportar em CSV ([#1](https://github.com/acme/demo/issues/1)) — PR [#10]", changelog)
        self.assertEqual(changelog.count("PR [#10]"), 1)
        state = box.state()
        for number in (10, 1):
            self.assertEqual(len(_marks(state, number, "dev-workflow:released v1.3.0")), 1)

    def test_without_github_each_rebased_commit_is_a_change_of_its_own(self):
        box = self.box
        commits = [("feat: add the export command", None), ("fix: escape the separator", None)]
        box.merge_pr(10, "feat: add the export command", commits, labels=["release"], rebase=True)
        completed = box.run("release.py", "plan", "--format", "json", env={"DEV_WORKFLOW_OFFLINE": "1"})
        plan = json.loads(self.assertRan(completed))
        self.assertEqual([change["kind"] for change in plan["changes"]], ["commit", "commit"])
        self.assertEqual(plan["next_version"], "1.3.0")
        self.assertFalse(plan["requested"])  # sem a API não há label, e nada pede versão


class IssueSuffixTest(SandboxCase):
    """``(#N)`` no fim do assunto é o sufixo do squash, mas N pode ser uma issue."""

    def _direct_commit(self, subject):
        box = self.box
        box.write({"hotfix.txt": subject})
        box.git("add", "-A")
        box.git("commit", "-q", "-m", subject)
        box.git("push", "-q", "origin", "main")

    def test_a_suffix_that_points_to_an_issue_is_not_taken_for_a_pr(self):
        box = self.box
        box.issue(1, "Corrigir a lista vazia", "bug")
        self._direct_commit("fix: handle the empty list (#1)")
        plan = json.loads(self.assertRan(box.run("release.py", "plan", "--format", "json")))
        [change] = plan["changes"]
        self.assertEqual((change["kind"], change["pr"]), ("commit", None))
        self.assertEqual([issue["number"] for issue in change["issues"]], [1])
        self.assertFalse(change["issues"][0]["closing"])  # o sufixo cita, nunca fecha

        self.assertRan(box.release("--force"))
        self.assertEqual(box.tags(), ["v1.2.0", "v1.2.1"])
        changelog = box.read("CHANGELOG.md")
        self.assertNotIn("PR [#1]", changelog)
        self.assertNotIn("/pull/1)", changelog)
        self.assertIn("— parte de [#1](https://github.com/acme/demo/issues/1)", changelog)
        state = box.state()
        self.assertEqual([b for b in _bodies(state, 1) if "Este PR foi publicado" in b], [])

    def test_a_suffix_of_a_pr_that_was_closed_without_merge_is_not_a_pr_change(self):
        box = self.box
        box.update_state(
            lambda state: state["pulls"].__setitem__(
                "7", {"number": 7, "title": "feat: abandoned", "state": "closed", "merged": False, "labels": [{"name": "release"}]}
            )
        )
        self._direct_commit("fix: handle the empty list (#7)")
        plan = json.loads(self.assertRan(box.run("release.py", "plan", "--format", "json")))
        self.assertEqual([change["kind"] for change in plan["changes"]], ["commit"])
        self.assertFalse(plan["requested"])  # o label do PR não mesclado não pede versão

    def test_without_github_the_suffix_is_trusted_as_a_pr(self):
        box = self.box
        self._direct_commit("fix: handle the empty list (#1)")
        completed = box.run("release.py", "plan", "--format", "json", env={"DEV_WORKFLOW_OFFLINE": "1"})
        [change] = json.loads(self.assertRan(completed))["changes"]
        self.assertEqual((change["kind"], change["pr"]), ("pr", 1))


class PaginationTest(SandboxCase):
    """As leituras que decidem marcações e níveis passam da primeira página da API."""

    def test_more_than_100_comments_on_a_pr_do_not_duplicate_the_release_mark(self):
        box = self.box
        box.issue(1, "Corrigir a lista vazia", "bug")
        box.merge_pr(10, "fix: handle the empty list", FIX, labels=["release"], closes=[1])
        box.fill_comments(10, 150)  # a marcação desta release vai cair na segunda página
        box.fill_comments(1, 150)
        self.assertRan(box.release())
        state = box.state()
        self.assertEqual((len(_marks(state, 10)), len(_marks(state, 1))), (1, 1))
        self.assertGreater(len(_bodies(state, 10)), 100)

        # Retomada: a tag e a GitHub Release somem e a publicação refaz as marcações.
        box.git("tag", "-d", "v1.2.1")
        box.git("push", "-q", "origin", ":refs/tags/v1.2.1")
        box.update_state(lambda state: state["releases"].clear())
        out = self.assertRan(box.release())
        self.assertIn("Commit de release de v1.2.1 sem tag; publicando.", out)
        state = box.state()
        self.assertEqual((len(_marks(state, 10)), len(_marks(state, 1))), (1, 1))
        self.assertIn("GET issues/10/comments page=2", state["pages"])

    def test_an_existing_milestone_beyond_the_first_page_is_reused(self):
        box = self.box
        box.update_state(
            lambda state: state.__setitem__(
                "milestones", [{"number": n, "title": f"m{n}", "state": "closed"} for n in range(1, 150)]
                + [{"number": 150, "title": "v1.2.1", "state": "open"}]
            )
        )
        box.merge_pr(10, "fix: handle the empty list", FIX, labels=["release"])
        self.assertRan(box.release())
        state = box.state()
        self.assertEqual(len(state["milestones"]), 150)
        self.assertEqual(state["milestones"][-1], {"number": 150, "title": "v1.2.1", "state": "closed"})
        self.assertEqual(state["pulls"]["10"]["milestone"], {"number": 150})

    def test_an_analysis_recorded_after_100_comments_still_counts(self):
        box = self.box
        box.merge_pr(10, "fix: handle the empty list", FIX, labels=["release"])
        box.fill_comments(10, 120)
        box.comment(
            10,
            "<!-- dev-workflow:analysis\n"
            f'{{"schema": 1, "base": "v1.2.0", "head": "{box.head_of(10)}", "changes": '
            '[{"pr": 10, "level": "major", "summary": "fetch() exige um argumento", "evidence": []}]}\n-->\nanálise',
        )
        self.assertRan(box.release())
        self.assertEqual(box.tags(), ["v1.2.0", "v2.0.0"])

    def test_more_than_100_commits_on_a_pr_are_all_checked(self):
        box = self.box
        box.issue(1, "Exportar em CSV", "feature")
        box.open_pr(11, "feat: add the export command", FEAT, closes=[1])
        fake = [{"sha": f"{n:040x}", "commit": {"message": f"feat: step {n}"}} for n in range(140)]
        fake.append({"sha": "f" * 40, "commit": {"message": "chore(release): v9.9.9"}})
        box.update_state(lambda state: state["pulls"]["11"].update(commit_list=fake))
        completed = box.run("github_issues.py", "check-pr", "--pr", "11")
        self.assertEqual(completed.returncode, 1, completed.stdout)
        self.assertIn("O commit `chore(release): v9.9.9` tem o formato do commit de release", completed.stdout)

    def test_the_evidence_marker_on_a_later_page_is_not_posted_again(self):
        box = self.box
        box.issue(1, "Exportar em CSV", "feature")
        box.merge_pr(10, "feat: add the export command", FEAT, closes=[1])
        box.fill_comments(1, 130)
        self.assertRan(box.run("github_issues.py", "pr-merged", "--pr", "10"))
        self.assertRan(box.run("github_issues.py", "pr-merged", "--pr", "10"))
        evidence = [b for b in _bodies(box.state(), 1) if "dev-workflow:evidence pr=10" in b]
        self.assertEqual(len(evidence), 1)


class ChangelogHideTest(SandboxCase):
    config = BASE_CONFIG + '\n[changelog]\nhide = ["maintenance", "docs"]\n'

    def test_hidden_sections_stay_out_of_the_file_but_not_out_of_the_notes(self):
        box = self.box
        box.issue(1, "Corrigir a lista vazia", "bug")
        box.merge_pr(10, "chore: bump the linter", [("chore: bump the linter", None)])
        box.merge_pr(11, "docs: explain the export", [("docs: explain the export", None)])
        box.merge_pr(12, "fix: handle the empty list", FIX, labels=["release"], closes=[1])
        self.assertRan(box.release())
        changelog = box.read("CHANGELOG.md")
        self.assertIn("### Correções", changelog)
        for hidden in ("### Manutenção", "bump the linter", "### Documentação", "explain the export"):
            self.assertNotIn(hidden, changelog)
        notes = box.state()["releases"]["v1.2.1"]["body"]
        for shown in ("### Correções", "### Manutenção", "bump the linter", "### Documentação"):
            self.assertIn(shown, notes)
        self.assertIn("### Manutenção", box.git("tag", "-l", "--format=%(contents)", "v1.2.1"))

    def test_a_release_with_only_hidden_sections_says_nothing_is_visible(self):
        box = self.box
        box.merge_pr(10, "chore: bump the linter", [("chore: bump the linter", None)], labels=["release"])
        self.assertRan(box.release())
        self.assertEqual(box.tags(), ["v1.2.0", "v1.2.1"])
        changelog = box.read("CHANGELOG.md")
        self.assertIn("Sem mudanças visíveis para quem usa o projeto.", changelog)
        self.assertNotIn("bump the linter", changelog)

    def test_hiding_the_breaking_section_keeps_the_major(self):
        box = self.box
        box.write({".github/dev-workflow.toml": BASE_CONFIG + '\n[changelog]\nhide = ["breaking"]\n'})
        box.git("commit", "-q", "-am", "chore: hide the breaking section")
        box.git("push", "-q", "origin", "main")
        box.merge_pr(10, "feat!: drop the legacy client", [("feat!: drop the legacy client", None)], labels=["release"])
        self.assertRan(box.release())
        self.assertEqual(box.tags(), ["v1.2.0", "v2.0.0"])
        changelog = box.read("CHANGELOG.md")
        self.assertNotIn("### ⚠ Mudanças incompatíveis", changelog)
        self.assertIn("### ⚠ Mudanças incompatíveis", box.state()["releases"]["v2.0.0"]["body"])


class ReleaseMarkTest(SandboxCase):
    """``release.mark``: comentário, milestone, os dois (padrão) ou nenhum."""

    def _release_with(self, mark):
        box = self.box
        box.write({".github/dev-workflow.toml": BASE_CONFIG + f"\n[release]\nmark = {json.dumps(mark)}\n"})
        box.git("commit", "-q", "-am", "chore: choose the release marks")
        box.git("push", "-q", "origin", "main")
        box.issue(1, "Corrigir a lista vazia", "bug")
        box.merge_pr(10, "fix: handle the empty list", FIX, labels=["release"], closes=[1])
        self.assertRan(box.release())
        self.assertEqual(box.remote_tags(), ["v1.2.0", "v1.2.1"])
        self.assertIn("v1.2.1", box.state()["releases"])
        return box.state()

    def test_only_comments(self):
        state = self._release_with(["comment"])
        self.assertEqual((len(_marks(state, 10)), len(_marks(state, 1))), (1, 1))
        self.assertEqual(state["milestones"], [])
        self.assertIsNone(state["pulls"]["10"]["milestone"])
        self.assertNotIn("GET milestones", state["log"])

    def test_only_the_milestone(self):
        state = self._release_with(["milestone"])
        self.assertEqual((_marks(state, 10), _marks(state, 1)), ([], []))
        self.assertEqual(state["milestones"], [{"number": 1, "title": "v1.2.1", "state": "closed"}])
        self.assertEqual((state["pulls"]["10"]["milestone"], state["issues"]["1"]["milestone"]), ({"number": 1},) * 2)

    def test_nothing(self):
        state = self._release_with([])
        self.assertEqual((_marks(state, 10), _marks(state, 1), state["milestones"]), ([], [], []))
        writes = [entry for entry in state["log"] if entry.startswith(("POST", "PATCH")) and "releases" not in entry]
        self.assertEqual(writes, [])  # só a GitHub Release; lê comentários (análises), não escreve

    def test_an_issue_already_in_a_milestone_keeps_it(self):
        box = self.box
        box.issue(1, "Corrigir a lista vazia", "bug")
        box.update_state(lambda state: state["issues"]["1"].update(milestone={"number": 99}))
        box.merge_pr(10, "fix: handle the empty list", FIX, labels=["release"], closes=[1])
        self.assertRan(box.release())
        state = box.state()
        self.assertEqual(state["issues"]["1"]["milestone"], {"number": 99})
        self.assertEqual(len(_marks(state, 1)), 1)

    def test_prereleases_are_not_marked(self):
        box = self.box
        box.merge_pr(10, "fix: handle the empty list", FIX, labels=["release:rc"])
        self.assertRan(box.release())
        self.assertEqual(box.tags(), ["v1.2.0", "v1.2.1-rc.1"])
        state = box.state()
        self.assertEqual((_bodies(state, 10), state["milestones"]), ([], []))


class StrictZeroMajorTest(SandboxCase):
    config = BASE_CONFIG.replace('initial_version = "1.2.0"', 'initial_version = "0.5.0"\nzero_major = "strict"')
    tag = "v0.5.0"
    files = {
        "pyproject.toml": '[project]\nname = "demo"\nversion = "0.5.0"\n',
        "package.json": '{\n  "name": "demo",\n  "version": "0.5.0"\n}\n',
    }

    def test_a_breaking_change_in_0x_goes_to_1_0_0(self):
        box = self.box
        box.merge_pr(10, "feat!: drop the legacy client", [("feat!: drop the legacy client", None)], labels=["release"])
        out = self.assertRan(box.release())
        self.assertNotIn("Versão 0.x", out)
        self.assertEqual(box.tags(), ["v0.5.0", "v1.0.0"])
        self.assertIn('version = "1.0.0"', box.read("pyproject.toml"))
        self.assertIn("### ⚠ Mudanças incompatíveis", box.read("CHANGELOG.md"))
        self.assertEqual(self.assertRan(box.run("release.py", "next", "--level", "major")).strip(), "2.0.0")

    def test_features_and_fixes_in_0x_follow_the_usual_levels(self):
        box = self.box
        self.assertEqual(self.assertRan(box.run("release.py", "next", "--level", "major")).strip(), "1.0.0")
        box.merge_pr(10, "feat: add the export command", FEAT, labels=["release"])
        self.assertRan(box.release())
        box.merge_pr(11, "fix: handle the empty list", FIX, labels=["release"])
        self.assertRan(box.release())
        self.assertEqual(box.tags(), ["v0.5.0", "v0.6.0", "v0.6.1"])

    def test_the_preview_says_1_0_0_and_a_prerelease_targets_it(self):
        box = self.box
        branch = box.open_pr(11, "feat!: drop the legacy client", [("feat!: drop the legacy client", None)], labels=["release"])
        out = self.assertRan(box.run("release.py", "preview", "--pr", "11", "--base-ref", "main", "--head-ref", branch))
        self.assertIn("ao mesclar, este PR publica **v1.0.0** (major)", out)
        box.merge_pr(12, "feat!: rename the flags", [("feat!: rename the flags", None)])
        self.assertRan(box.release("--prerelease", "rc"))
        self.assertIn("v1.0.0-rc.1", box.tags())


if __name__ == "__main__":
    unittest.main()
