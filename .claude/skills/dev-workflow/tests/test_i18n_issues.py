"""Idioma das mensagens de issues, PR e epics (#37): catraca, ``check-pr``, ``pr-merged`` e fechamento do epic."""

import contextlib
import io
import json
import re
import subprocess
import unittest

import support  # noqa: F401  (põe scripts/ no sys.path antes de importar devflow)
from devflow import i18n
from devflow.config import Config
from devflow.epics import IssueLinks, close_completed_parents, epic_summary
from devflow.issues import check_pull_request
from i18n_support import PR_BODY_EN, accented_literals, english_config
from support import SandboxCase

MODULES = ("issues.py", "text.py", "epics.py")
ACCENTED = re.compile(r"[À-ÿ]")
REPO = "acme/demo"


class RatchetTest(unittest.TestCase):
    def test_no_accented_text_outside_docstrings(self):
        for module in MODULES:
            with self.subTest(module):
                self.assertEqual(accented_literals(module), [])


class EnglishCheckTest(unittest.TestCase):
    """``check_pull_request`` direto, com a configuração em inglês."""

    def test_the_findings_are_in_english(self):
        cfg = Config({"language": "en"})
        pr = {
            "number": 9,
            "title": "add the export command",
            "body": "## Summary\n- x\n",
            "labels": [{"name": "semver:minor"}, {"name": "semver:major"}],
            "user": {"login": "alice"},
        }
        findings = check_pull_request(pr, REPO, lambda number: None, cfg)
        text = "\n".join(findings.errors + findings.warnings)
        self.assertIn("The PR body references no issue", text)
        self.assertIn("The PR body has no `## Tests run` section (use the PR template).", text)
        self.assertIn("More than one override label (major, minor); keep only one.", text)
        self.assertIn("does not follow Conventional Commits", text)
        self.assertIsNone(ACCENTED.search(text), text)

    def test_portuguese_is_still_the_default(self):
        pr = {"number": 9, "title": "feat: x", "body": "", "labels": [], "user": {"login": "alice"}}
        errors = check_pull_request(pr, REPO, lambda number: None, Config()).errors
        self.assertIn(
            "O corpo do PR não referencia issue: use `Closes #N` (entrega completa) ou `Refs #N` (parcial).", errors
        )


class EnglishPullRequestFlowTest(SandboxCase):
    config = english_config()

    def _body(self, issues):
        return PR_BODY_EN.format(issues=issues, summary="add the export command", version="New capability")

    def test_check_pr_speaks_english(self):
        box = self.box
        box.issue(1, "Export as CSV", "feature")
        box.open_pr(11, "feat: add the export command", [("feat: add the export command", None)])
        box.update_state(
            lambda state: state["pulls"]["11"].update(body=self._body("Closes #1"), labels=[{"name": "semver:patch"}])
        )
        completed = box.run("github_issues.py", "check-pr", "--pr", "11")
        self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
        self.assertIn(
            "ERROR: `semver:patch` only applies to a PR labeled `release` or with a channel label (`release:alpha`, "
            "`release:beta`, `release:rc`): the override decides the number of the version this PR publishes.",
            completed.stdout,
        )
        self.assertIn("ERROR: Version override without a reason: write `Override reason: <reason>`", completed.stdout)
        self.assertIn("warning: #1 is missing template sections: Context, Goal", completed.stdout)
        self.assertIsNone(ACCENTED.search(completed.stdout), completed.stdout)

        box.update_state(lambda state: state["pulls"]["11"].update(labels=[]))
        completed = box.run("github_issues.py", "check-pr", "--pr", "11")
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("PR #11 checked: 1 warning(s).", completed.stdout)

    def test_pr_merged_writes_the_evidence_in_english(self):
        box = self.box
        box.issue(1, "Export as CSV", "feature", body="## Acceptance criteria\n- [x] exports\n- [ ] refuses empty\n")
        box.issue(2, "Document the export", "documentation")
        box.merge_pr(
            10,
            "feat: add the export command",
            [("feat: add the export command", None)],
            labels=("release",),
            body=self._body("Closes #1\nRefs #2"),
        )
        completed = box.run("github_issues.py", "pr-merged", "--pr", "10")
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("#1: evidence from PR #10 recorded.", completed.stdout)
        comments = box.state()["comments"]
        closing, partial = comments["1"][0]["body"], comments["2"][0]["body"]
        self.assertTrue(closing.startswith("<!-- dev-workflow:evidence pr=10 -->\n"), closing)
        for expected in (
            "**Delivered by PR #10**, merged by @alice.",
            "**Commits**",
            "feat: add the export command",
            "**Tests run** (as stated in the PR)\n- `make test` — 12 passed",
            "**Documentation** (as stated in the PR)\n- Not applicable — no change in usage",
            "**Acceptance criteria:** 1 of 2 unchecked at merge time:\n- [ ] refuses empty",
            "**Version:** the PR is labeled for a release",
        ):
            self.assertIn(expected, closing)
        self.assertIn("**Partial delivery by PR #10** (the PR cites this issue with `Refs`; it stays open).", partial)
        for body in (closing, partial):
            self.assertIsNone(ACCENTED.search(body), body)

        again = box.run("github_issues.py", "pr-merged", "--pr", "10")
        self.assertIn("#1: the evidence from PR #10 is already recorded.", again.stdout)


def _node(number, *, parent=None, sub_issues=(), closed=False, title=""):
    def ref(value):
        return {"number": value, "repository": {"nameWithOwner": REPO}}

    return {
        "__typename": "Issue",
        "number": number,
        "title": title or f"Issue {number}",
        "state": "CLOSED" if closed else "OPEN",
        "stateReason": "COMPLETED" if closed else None,
        "parent": ref(parent) if parent else None,
        "subIssues": {"nodes": [ref(child) for child in sub_issues]},
        "blockedBy": {"nodes": []},
    }


class _Gh:
    """O mínimo do ``gh`` para o fechamento automático: GraphQL, ``issue view`` e ``issue close``."""

    def __init__(self, issues):
        self.issues = issues
        self.closes = []

    def __call__(self, args):
        args = list(args)
        if args[:2] == ["api", "graphql"]:
            numbers = [int(n) for n in re.findall(r"\bi(\d+):", args[-1])]
            data = {"repository": {f"i{n}": self.issues.get(n) for n in numbers}}
            return subprocess.CompletedProcess(args, 0, json.dumps({"data": data}), "")
        if args[:2] == ["issue", "view"]:
            node = self.issues[int(args[2])]
            data = {"state": node["state"], "labels": [{"name": "epic"}]}
            return subprocess.CompletedProcess(args, 0, json.dumps(data), "")
        if args[:2] == ["issue", "close"]:
            self.closes.append((int(args[2]), args[args.index("--reason") + 1], args[args.index("--comment") + 1]))
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(args)


class EpicClosingCommentTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(i18n.activate, i18n.active())

    def _children(self):
        return [
            IssueLinks(2, parent=1, closed=True, title="Export", state_reason="COMPLETED"),
            IssueLinks(3, parent=1, closed=True, title="Import", state_reason="NOT_PLANNED"),
        ]

    def test_the_portuguese_comment_is_unchanged(self):
        i18n.activate("pt-BR")
        self.assertEqual(
            epic_summary(self._children()),
            (
                "completed",
                "Fechado automaticamente pelo fluxo de epics: as 2 sub-issues estão fechadas.\n\n"
                "- #2 Export — concluída\n- #3 Import — dispensada\n\n"
                "Se algum critério de conclusão do epic ainda faltar, reabra o epic e registre o que falta.",
            ),
        )

    def test_the_epic_closes_with_an_english_comment(self):
        i18n.activate("en")
        gh = _Gh(
            {
                1: _node(1, sub_issues=(2, 3)),
                2: _node(2, parent=1, closed=True, title="Export"),
                3: _node(3, parent=1, closed=True, title="Import"),
            }
        )
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(close_completed_parents(3, REPO, dry_run=False, gh=gh), 0)
        self.assertEqual(
            gh.closes,
            [
                (
                    1,
                    "completed",
                    "Closed automatically by the epic flow: all 2 sub-issues are closed.\n\n"
                    "- #2 Export — completed\n- #3 Import — completed\n\n"
                    "If any completion criterion of the epic is still missing, reopen the epic and record what is left.",
                )
            ],
        )
        self.assertIn("#1 closed as completed.", out.getvalue())


if __name__ == "__main__":
    unittest.main()
