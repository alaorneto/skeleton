"""Idioma de ``release.py``: catraca do texto fixo e uma release completa em inglês no sandbox.

A release em inglês passa por tudo que ``release.py`` escreve: mensagens de execução, notas da
GitHub Release, tag anotada, changelog, prévia comentada no PR, PR de release e os comentários
"publicado na versão". Reexecutar não duplica nada, porque as marcas ``<!-- dev-workflow:… -->``
não mudam com o idioma.
"""

import unittest

import support  # noqa: F401  (põe scripts/ no sys.path antes de importar devflow)
from devflow import i18n
from devflow.config import Config
from devflow.plan import Change, Links, Plan
from devflow.release import PREVIEW_MARK, RELEASE_PR_MARK, analysis_comment, fit_notes, release_notes, report
from devflow.semver import Version
from i18n_support import PR_BODY_EN, accented_literals, english_config
from support import Sandbox, SandboxCase

FIX = [("fix: handle the empty list", None)]
FEAT = [("feat: add the export command", None)]
# Textos em português que ``release.py`` escrevia; nenhum pode sobrar numa release em inglês.
PORTUGUESE = (
    "Por que este número",
    "publicado na versão",
    "Entregue na versão",
    "Prévia de versão",
    "Atualizado a cada push",
    "PR de release",
    "Próxima versão",
    "Nada a versionar",
    "criada em",
)


def _body(title, closes=()):
    issues = "\n".join(f"Closes #{n}" for n in closes) or "Closes #"
    return PR_BODY_EN.format(issues=issues, summary=title, version="No declared impact")


def _bodies(state, number):
    return [comment["body"] for comment in state["comments"].get(str(number), [])]


def _change(number, fresh=True):
    return Change("pr", number, f"{number:040x}", f"feat: change {number}", labels={"release"}, fresh=fresh)


def _plan(changes, version, *, prerelease=None, previous_tag=None, cycle=()):
    return Plan(
        branch="main",
        head="0" * 40,
        base_tag="v1.2.0",
        base_version=Version.parse("1.2.0"),
        files_version=Version.parse("1.2.0"),
        changes=list(changes),
        requested_by=[],
        forced=True,
        natural="minor",
        level="minor",
        override=None,
        version=Version.parse(version),
        prerelease=prerelease,
        previous_tag=previous_tag,
        cycle=list(cycle),
    )


class RatchetTest(unittest.TestCase):
    def test_release_has_no_portuguese_left_outside_the_catalog(self):
        # `—` é o separador do título `## [x.y.z] — data`, lido por código; `⚠` marca a mudança incompatível.
        self.assertEqual(accented_literals("release.py", allowed="—⚠"), [])


class EnglishNotesTest(unittest.TestCase):
    cfg = Config({"language": "en"})
    links = Links("https://github.com/acme/demo")

    def test_notes_that_do_not_fit_are_cut_in_english(self):
        changelog = "\n".join(f"- entry {n}" for n in range(50))
        cut = fit_notes(changelog, "| a |\n" * 200, "short", "", 200, full_list="in x", language="en")
        self.assertLessEqual(len(cut), 200)
        self.assertIn("more entry(ies): the full list is in x._", cut)
        self.assertTrue(cut.endswith("### Why this number\n\nshort\n"), cut)

    def test_the_full_list_pointer_and_the_table_note_follow_the_language(self):
        plan = _plan([_change(number) for number in range(1, 61)], "1.3.0")
        cut = release_notes(plan, self.cfg, self.links, limit=2500, ref="v1.3.0")
        self.assertLessEqual(len(cut), 2500)
        self.assertIn(
            "the full list is in [CHANGELOG.md](https://github.com/acme/demo/blob/v1.3.0/CHANGELOG.md)._", cut
        )
        prerelease = _plan(plan.changes, "1.3.0-rc.1", prerelease="rc", previous_tag="v1.2.0")
        cut = release_notes(prerelease, self.cfg, self.links, limit=2500, ref="v1.3.0-rc.1")
        self.assertIn("the full list is in the message of the annotated tag._", cut)
        self.assertIn("does not fit here", cut)
        self.assertNotIn("Por que este número", cut)

    def test_the_plan_report_is_in_english(self):
        plan = _plan([_change(1)], "1.3.0")
        self.assertIn("### Next version: **v1.3.0** (minor) — will be published", report(plan, self.cfg, self.links))
        plan.forced, plan.problems = False, ["the floor is not reached"]
        text = report(plan, self.cfg, self.links)
        self.assertIn("— would be published (no PR has asked for a version yet)", text)
        self.assertIn("**Problems that block the release**\n- the floor is not reached", text)
        plan.version = None
        self.assertIn("Nothing accumulated since v1.2.0.", report(plan, self.cfg, self.links))

    def test_the_analysis_comment_is_in_english(self):
        self.addCleanup(i18n.activate, i18n.active())
        i18n.activate("en")
        data = {
            "base": "",
            "head": "abc1234def",
            "level": "minor",
            "changes": [{"pr": 3, "level": "minor", "breaking": False, "summary": "adds a flag", "evidence": []}],
            "notes": "",
        }
        text = analysis_comment(data)
        self.assertIn("### Version analysis\nRange `start..abc1234` · proposed level: **minor**", text)
        self.assertIn("| Change | Level | Reason |", text)
        self.assertIn("<!-- dev-workflow:analysis", text)


class EnglishReleaseTest(SandboxCase):
    config = english_config()

    def test_a_complete_release_in_english_and_a_rerun_that_changes_nothing(self):
        box = self.box
        box.issue(1, "Handle the empty list", "bug")
        box.issue(2, "Export as CSV", "feature")
        box.merge_pr(10, "fix: handle the empty list", FIX, closes=[1], body=_body("fix", closes=[1]))

        # prévia de um PR aberto: um comentário, atualizado no lugar
        branch = box.open_pr(11, "feat: add the export command", FEAT, labels=["release"], body=_body("feat"))
        preview = ("release.py", "preview", "--pr", "11", "--base-ref", "main", "--head-ref", branch, "--comment")
        out = self.assertRan(box.run(*preview))
        self.assertIn("### Version preview: when merged, this PR publishes **v1.3.0** (minor)", out)
        self.assertIn("This version includes this PR and 1 change(s) accumulated since v1.2.0.", out)
        box.update_state(lambda state: state["pulls"]["11"].update(labels=[]))
        out = self.assertRan(box.run(*preview))
        self.assertIn("### Version preview: this PR publishes no version", out)
        self.assertIn("With `release`, merging would publish **v1.3.0** (minor).", out)
        (comment,) = _bodies(box.state(), 11)
        self.assertTrue(comment.startswith(PREVIEW_MARK + "\n"), comment)
        self.assertIn("<sub>Updated on every push and every label change.</sub>", comment)

        # a release: mensagens, changelog, tag, GitHub Release e marcações
        box.merge_pr(12, "feat: add the export command", FEAT, labels=["release"], closes=[2], body=_body("feat", [2]))
        out = self.assertRan(box.release())
        self.assertIn("### Next version: **v1.3.0** (minor) — will be published", out)
        self.assertIn("Tag v1.3.0 created on ", out)
        self.assertIn("GitHub Release v1.3.0 created.", out)
        changelog = box.read("CHANGELOG.md")
        self.assertTrue(changelog.startswith("# Changelog\n\nAll published changes of this project."), changelog)
        self.assertLess(changelog.index("### Features"), changelog.index("### Fixes"))
        self.assertIn("## [1.3.0](https://github.com/acme/demo/compare/v1.2.0...v1.3.0) — ", changelog)
        self.assertIn("### Why this number", box.git("tag", "-l", "--format=%(contents)", "v1.3.0"))
        state = box.state()
        body = state["releases"]["v1.3.0"]["body"]
        self.assertIn("Export as CSV", body)
        self.assertIn("### Why this number", body)
        url = "https://github.com/acme/demo/releases/tag/v1.3.0"
        for number in (10, 12):
            self.assertEqual(
                _bodies(state, number),
                [f"<!-- dev-workflow:released v1.3.0 -->\n🚀 This PR was published in version [v1.3.0]({url}).\n"],
            )
        for number in (1, 2):
            self.assertEqual(
                _bodies(state, number),
                [f"<!-- dev-workflow:released v1.3.0 -->\n🚀 Delivered in version [v1.3.0]({url}).\n"],
            )
        written = [out, changelog, body, *(text for number in (10, 12, 1, 2) for text in _bodies(state, number))]
        for text in written:
            for portuguese in PORTUGUESE:
                self.assertNotIn(portuguese, text)

        # reexecutar em inglês não duplica nada
        head = box.git("rev-parse", "HEAD")
        again = self.assertRan(box.release())
        self.assertIn("Nothing to release: 0 change(s) accumulated since v1.3.0", again)
        after = box.state()
        self.assertEqual(box.git("rev-parse", "HEAD"), head)
        self.assertEqual(box.tags(), ["v1.2.0", "v1.3.0"])
        self.assertEqual((after["releases"], after["comments"]), (state["releases"], state["comments"]))


class EnglishReleasePullRequestTest(SandboxCase):
    config = english_config()
    mode = "pull-request"

    def test_the_release_pr_is_written_in_english_and_merging_it_publishes(self):
        box = self.box
        box.issue(1, "Handle the empty list", "bug")
        box.merge_pr(10, "fix: handle the empty list", FIX, labels=["release"], closes=[1], body=_body("fix", [1]))
        self.assertIn("Release PR #1001 opened for v1.2.1.", self.assertRan(box.release()))
        self.assertIn("Release PR #1001 updated to v1.2.1.", self.assertRan(box.release()))
        pull = box.state()["pulls"]["1001"]
        self.assertEqual(pull["title"], "chore(release): v1.2.1")  # o assunto é lido por código: não muda
        self.assertTrue(pull["body"].startswith(f"{RELEASE_PR_MARK}\nRelease PR generated by the automation."))
        self.assertIn("Do not edit it", pull["body"])
        self.assertIn("### Why this number", pull["body"])

        box.git("fetch", "-q", "origin", "release/next")
        box.git("merge", "-q", "--no-ff", "FETCH_HEAD", "-m", "Merge pull request #1001 from acme/release/next")
        box.git("push", "-q", "origin", "main")
        box.update_state(lambda state: state["pulls"]["1001"].update(state="closed", merged=True, merged_at="x"))
        out = self.assertRan(box.release())
        self.assertIn("Release commit of v1.2.1 has no tag yet; publishing.", out)
        self.assertEqual(box.remote_tags(), ["v1.2.0", "v1.2.1"])
        self.assertIn("🚀 This PR was published in version", _bodies(box.state(), 10)[0])
        self.assertIn("Nothing to release", self.assertRan(box.release()))
        self.assertEqual(len(_bodies(box.state(), 10)), 1)


class EnglishBaselineTest(unittest.TestCase):
    def test_the_baseline_tag_and_its_messages_are_in_english(self):
        box = Sandbox(english_config(), tag=None)
        self.addCleanup(box.close)
        out = box.run("release.py", "baseline", "--push")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("Base tag v1.2.0 created on ", out.stdout)
        self.assertIn("Tag pushed to the remote.", out.stdout)
        message = box.git("tag", "-l", "--format=%(contents)", "v1.2.0")
        self.assertIn("Baseline of the automatic versioning: what came before this tag is version 1.2.0.", message)
        self.assertIn("already has version tags", box.run("release.py", "baseline").stdout)


if __name__ == "__main__":
    unittest.main()
