"""Complete pull request check and evidence comment on merge (#212)."""

import io
import json
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "scripts"))

from github_issues import (  # noqa: E402
    EVIDENCE_MARKER,
    PullRequestView,
    check_pull_request,
    closing_issues,
    exemption,
    markdown_sections,
    pr_merged,
)
from workflow_config import DEFAULT  # noqa: E402

REPO = "owner/project"
TEMPLATE = (SKILL / "assets" / "pull_request_template.md").read_text(encoding="utf-8")
ISSUE_BODY = "## Contexto\nx\n\n## Critérios de aceite\n- [x] feito\n- [ ] falta isto\n\n## Definição de pronto\n- [ ] CI verde\n"
GOOD_BODY = """## Issues
Closes #10

## Resumo
- Corrige a busca.

## Versão
- acumulado

## Testes executados
- `pytest tests/test_search.py` — 4 passed

## Documentação
- docs/search.md
"""


def fetcher(**issues):
    defaults = {10: {"labels": [{"name": "bug"}], "body": ISSUE_BODY}}
    defaults.update({int(key[1:]): value for key, value in issues.items()})
    return defaults.get


def check(body=GOOD_BODY, title="fix(search): corrige a busca", labels=(), config=DEFAULT, fetch=None):
    pull = PullRequestView(title=title, body=body, labels=tuple(labels))
    return check_pull_request(pull, REPO, fetch or fetcher(), config, TEMPLATE)


class CheckPullRequestTest(unittest.TestCase):
    def test_complete_pull_request_without_release_passes_and_warns_pending_criteria(self):
        errors, warnings = check()
        self.assertEqual(errors, [])
        self.assertEqual(warnings, ["#10 tem 1 critério(s) de aceite não marcado(s)."])

    def test_empty_or_template_only_section_fails(self):
        for body, name in (
            (GOOD_BODY.replace("- Corrige a busca.", ""), "Resumo"),
            (
                GOOD_BODY.replace("- `pytest tests/test_search.py` — 4 passed", "<!-- só comentário -->"),
                "Testes executados",
            ),
            (GOOD_BODY.replace("- acumulado", "- <acumulado | release>"), "Versão"),
            (GOOD_BODY.replace("## Documentação\n- docs/search.md\n", ""), "Documentação"),
        ):
            with self.subTest(section=name):
                errors, _ = check(body)
                self.assertTrue(any(f"## {name}" in error for error in errors), errors)

    def test_semver_override_rules(self):
        reason = GOOD_BODY + "\nMotivo: primeira versão estável\n"
        self.assertEqual(check(reason, labels=("release", "semver:major"))[0], [])
        self.assertTrue(any("só vale com o label" in e for e in check(reason, labels=("semver:minor",))[0]))
        self.assertTrue(any("exige uma linha" in e for e in check(labels=("release", "semver:minor"))[0]))
        self.assertTrue(
            any("No máximo um" in e for e in check(reason, labels=("release", "semver:minor", "semver:patch"))[0])
        )

    def test_title_must_be_conventional(self):
        errors, _ = check(title="Corrige a busca")
        self.assertTrue(any("Conventional Commits" in error for error in errors))
        self.assertEqual(check(title="Corrige a busca", config=replace(DEFAULT, pr_title_conventional=False))[0], [])

    def test_warnings_for_missing_issue_sections_and_type_mismatch(self):
        fetch = fetcher(n10={"labels": [{"name": "bug"}], "body": "## Contexto\nx\n"})
        errors, warnings = check(title="feat(search): nova busca", fetch=fetch)
        self.assertEqual(errors, [])
        self.assertTrue(any("não tem as seções Critérios de aceite, Definição de pronto" in w for w in warnings))
        self.assertTrue(any("`feat` (feature), mas #10 é bug" in w for w in warnings))

    def test_pending_criteria_can_be_an_error(self):
        errors, warnings = check(config=replace(DEFAULT, pr_pending_criteria="error"))
        self.assertTrue(any("critério(s) de aceite" in error for error in errors))
        self.assertFalse(any("critério(s) de aceite" in warning for warning in warnings))

    def test_issue_link_rules_still_apply(self):
        errors, _ = check(GOOD_BODY.replace("Closes #10", "Nada"))
        self.assertTrue(any("não referencia issue" in error for error in errors))

    def test_dependabot_and_release_pull_requests_are_exempt(self):
        self.assertIsNotNone(exemption(PullRequestView("chore(deps): bump", "", author="dependabot[bot]"), DEFAULT))
        self.assertIsNotNone(exemption(PullRequestView("chore(release): v0.6.0", "", head="release/v0.6.0"), DEFAULT))
        self.assertIsNone(exemption(PullRequestView("chore(release): v0.6.0", "", head="feat/1-x"), DEFAULT))
        self.assertIsNone(exemption(PullRequestView("fix: x", "", author="someone"), DEFAULT))

    def test_sections_and_closing_references(self):
        self.assertEqual(markdown_sections("## A\nx\n## B\r\ny\r\n")["b"], "y")
        self.assertEqual(closing_issues("Closes #1\nRefs #2\nFixes other/repo#3\nresolves #4", REPO), [1, 4])


class FakeGh:
    def __init__(self, *, permissions=None):
        self.comments = {10: [], 11: []}
        self.permissions = permissions or {}
        self.posts = []

    def __call__(self, args):
        args = list(args)
        ok = lambda data: subprocess.CompletedProcess(args, 0, json.dumps(data), "")  # noqa: E731
        path = next(arg for arg in args if arg.startswith("repos/"))
        if path.endswith("/pulls/5"):
            return ok(
                {
                    "number": 5,
                    "title": "fix: busca",
                    "merged_at": "2026-11-13",
                    "body": GOOD_BODY + "Closes #11\nRefs #12\n",
                    "labels": [{"name": "release"}],
                }
            )
        if path.endswith("/pulls/5/commits"):
            return ok([{"sha": "a" * 40, "commit": {"message": "fix: busca\n\nRefs #10"}}])
        if "/collaborators/" in path:
            return ok({"permission": self.permissions.get(path.split("/")[4], "read")})
        number = int(path.split("/")[4])
        if path.endswith("/comments"):
            if "-X" in args:
                self.posts.append(number)
                self.comments[number].append(
                    {"body": args[-1].split("=", 1)[1], "user": {"login": "github-actions[bot]"}}
                )
                return ok({})
            return ok(self.comments[number])
        return ok({"number": number, "body": ISSUE_BODY, "labels": []})


class EvidenceTest(unittest.TestCase):
    def merged(self, gh, config=DEFAULT):
        with redirect_stdout(io.StringIO()):
            return pr_merged(5, REPO, config, gh)

    def test_one_evidence_comment_per_closed_issue_even_when_rerun(self):
        gh = FakeGh()
        config = replace(DEFAULT, versioning=replace(DEFAULT.versioning, enabled=True))
        self.assertEqual(self.merged(gh, config), 0)
        self.assertEqual(sorted(gh.posts), [10, 11])  # Refs #12 is not closed by the PR.
        body = gh.comments[10][0]["body"]
        self.assertIn(EVIDENCE_MARKER.format(number=5), body)
        self.assertIn("aaaaaaaaaaaa fix: busca", body)
        self.assertIn("`pytest tests/test_search.py` — 4 passed", body)
        self.assertIn("- [ ] falta isto", body)
        self.assertIn("publicada pelo workflow de release", body)
        self.assertEqual(self.merged(gh, config), 0)
        self.assertEqual(sorted(gh.posts), [10, 11])

    def test_marker_written_by_an_untrusted_author_does_not_suppress_the_comment(self):
        gh = FakeGh()
        gh.comments[10].append({"body": EVIDENCE_MARKER.format(number=5), "user": {"login": "stranger"}})
        self.merged(gh)
        self.assertIn(10, gh.posts)
        trusted = FakeGh(permissions={"maintainer": "write"})
        trusted.comments[10].append({"body": EVIDENCE_MARKER.format(number=5), "user": {"login": "maintainer"}})
        self.merged(trusted)
        self.assertNotIn(10, trusted.posts)


if __name__ == "__main__":
    unittest.main()
