"""Lacunas da issue #34 que não pedem repositório: chaves de validação da configuração e o ``gh`` falso.

O ``gh`` falso é parte da suíte: se ele não paginar como a API, os testes de paginação passam
por acaso. Por isso o comportamento dele também tem teste.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401  (põe scripts/ no sys.path antes de importar devflow)
from devflow.config import Config, ConfigError
from devflow.gh import GitHub
from support import FAKE_GH, REPOSITORY


class ConfigValidationGapsTest(unittest.TestCase):
    """As chaves que ``Config._validate`` recusa e que ``test_units`` ainda não cobria."""

    def assertRefused(self, data, key):
        with self.assertRaises(ConfigError) as raised:
            Config(data)
        self.assertIn(f"`{key}`", str(raised.exception))
        return str(raised.exception)

    def test_each_refused_key_is_named_in_the_message(self):
        sections = Config().changelog["sections"]
        cases = {
            "types": {"types": {}},
            "changelog.sections": {"changelog": {"sections": [s for s in sections if s["id"] != "breaking"]}},
            "types.bug.prefixes": {"types": {"bug": {"prefixes": []}}},
            "types.bug.section": {"types": {"bug": {"prefixes": ["fix"], "section": "nowhere"}}},
            "commits.levels.feat": {"commits": {"levels": {"feat": "huge"}}},
            "pr.title_pattern": {"pr": {"title_pattern": "(unclosed"}},
            "versioning.zero_major": {"versioning": {"zero_major": "loose"}},
            "versioning.files[0].path": {"versioning": {"files": [{"search": "{current}"}]}},
            "versioning.files[0].count": {"versioning": {"files": [{"path": "a", "search": "{current}", "count": 0}]}},
            "versioning.public_surfaces[0].globs": {"versioning": {"public_surfaces": [{"name": "api"}]}},
            "versioning.public_surfaces[0].on_add": {
                "versioning": {"public_surfaces": [{"globs": ["api/**"], "on_add": "explode"}]}
            },
            "versioning.floors[0].globs": {"versioning": {"floors": [{"regex": "v(.+)"}]}},
            "release.mark": {"release": {"mark": ["comment", "label"]}},
        }
        for key, data in cases.items():
            with self.subTest(key):
                self.assertRefused(data, key)

    def test_the_maintenance_section_is_required_too(self):
        sections = [s for s in Config().changelog["sections"] if s["id"] != "maintenance"]
        message = self.assertRefused({"changelog": {"sections": sections}}, "changelog.sections")
        self.assertIn("`maintenance`", message)

    def test_a_count_that_is_not_an_integer_is_refused(self):
        for value in ("2", 1.5, -1):
            with self.subTest(value):
                self.assertRefused(
                    {"versioning": {"files": [{"path": "a", "search": "{current}", "count": value}]}}, "versioning.files[0].count"
                )

    def test_an_invalid_floor_regex_is_refused_with_the_key(self):
        message = self.assertRefused(
            {"versioning": {"floors": [{"globs": ["*"], "regex": "(unclosed"}]}}, "versioning.floors[0].regex"
        )
        self.assertIn("não é uma expressão regular válida", message)

    def test_every_surface_event_accepts_the_levels_and_review(self):
        for event in ("on_add", "on_modify", "on_remove"):
            for action in ("none", "patch", "minor", "major", "review"):
                with self.subTest(event=event, action=action):
                    Config({"versioning": {"public_surfaces": [{"globs": ["api/**"], event: action}]}})
            with self.subTest(event=event, action="bad"):
                self.assertRefused(
                    {"versioning": {"public_surfaces": [{"globs": ["api/**"], event: "bad"}]}},
                    f"versioning.public_surfaces[0].{event}",
                )

    def test_release_mark_accepts_any_subset_of_comment_and_milestone(self):
        for mark in ([], ["comment"], ["milestone"], ["milestone", "comment"]):
            with self.subTest(mark):
                self.assertEqual(Config({"release": {"mark": mark}}).release["mark"], mark)

    def test_zero_major_accepts_both_policies(self):
        for policy in ("breaking-is-minor", "strict"):
            with self.subTest(policy):
                self.assertEqual(Config({"versioning": {"zero_major": policy}}).versioning["zero_major"], policy)

    def test_changelog_hide_takes_section_ids(self):
        self.assertEqual(Config({"changelog": {"hide": ["maintenance", "docs"]}}).changelog["hide"], ["maintenance", "docs"])

    # Defeito encontrado (não corrigido aqui, regra da #34): `changelog.hide` não é validado. Um id
    # que não existe em `changelog.sections` (erro de digitação) é aceito e não esconde nada, ao
    # contrário de `types.<tipo>.section`, que é conferido contra os mesmos ids.
    def test_changelog_hide_with_an_unknown_section_id_is_refused(self):
        self.assertRefused({"changelog": {"hide": ["maintenence"]}}, "changelog.hide")

    # Mesmo defeito: um texto no lugar da lista vira um conjunto de letras e também não esconde nada.
    def test_changelog_hide_as_a_string_is_refused(self):
        self.assertRefused({"changelog": {"hide": "maintenance"}}, "changelog.hide")


class FakeGhTest(unittest.TestCase):
    """O ``gh`` falso corta as listas em páginas e simula ``label``, ``issue`` e ``api graphql``."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="fake-gh-", ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.state_path = Path(self._tmp.name) / "github.json"
        comments = [{"id": n, "body": f"c{n}", "user": {"login": "bob"}} for n in range(250)]
        issue = {"number": 1, "title": "Epic", "body": "## Itens\n- [ ] #2\n", "state": "open", "labels": []}
        child = {"number": 2, "title": "Filha", "body": "", "state": "closed", "labels": [{"name": "bug"}]}
        self.write({
            "pulls": {"10": {"number": 10, "title": "x", "state": "open", "head": {"ref": "a"}}},
            "issues": {"1": issue, "2": child},
            "comments": {"1": comments},
            "releases": {},
            "milestones": [],
            "dispatches": [],
        })
        self.env = {**os.environ, "FAKE_GH_STATE": str(self.state_path), "DEV_WORKFLOW_GH": json.dumps([sys.executable, str(FAKE_GH)])}

    def write(self, state):
        self.state_path.write_text(json.dumps(state), encoding="utf-8")

    def state(self):
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def gh(self, *args):
        completed = subprocess.run(
            [sys.executable, str(FAKE_GH), *args], env=self.env, capture_output=True, text=True, encoding="utf-8"
        )
        return completed

    def test_a_list_without_paginate_is_only_the_requested_page(self):
        first = json.loads(self.gh("api", f"repos/{REPOSITORY}/issues/1/comments").stdout)
        self.assertEqual([c["id"] for c in first], list(range(30)))
        hundred = json.loads(self.gh("api", f"repos/{REPOSITORY}/issues/1/comments?per_page=500").stdout)
        self.assertEqual(len(hundred), 100)
        third = json.loads(self.gh("api", f"repos/{REPOSITORY}/issues/1/comments?per_page=100&page=3").stdout)
        self.assertEqual([c["id"] for c in third], list(range(200, 250)))
        self.assertEqual(json.loads(self.gh("api", f"repos/{REPOSITORY}/issues/1/comments?page=99").stdout), [])

    def test_paginate_with_slurp_returns_every_page(self):
        out = self.gh("api", f"repos/{REPOSITORY}/issues/1/comments?per_page=100", "--paginate", "--slurp").stdout
        self.assertEqual([len(page) for page in json.loads(out)], [100, 100, 50])
        self.assertEqual(len(self.state()["pages"]), 3)
        flat = json.loads(self.gh("api", f"repos/{REPOSITORY}/issues/1/comments?per_page=100", "--paginate").stdout)
        self.assertEqual(len(flat), 250)
        empty = json.loads(self.gh("api", f"repos/{REPOSITORY}/issues/2/comments", "--paginate", "--slurp").stdout)
        self.assertEqual(empty, [[]])

    def test_the_client_reads_past_the_first_page(self):
        previous = {key: os.environ.get(key) for key in ("FAKE_GH_STATE", "DEV_WORKFLOW_GH", "DEV_WORKFLOW_OFFLINE")}
        os.environ.update(FAKE_GH_STATE=self.env["FAKE_GH_STATE"], DEV_WORKFLOW_GH=self.env["DEV_WORKFLOW_GH"])
        os.environ.pop("DEV_WORKFLOW_OFFLINE", None)
        try:
            self.assertEqual(len(GitHub(REPOSITORY).comments(1)), 250)
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_labels_are_created_listed_and_need_force_to_change(self):
        self.assertEqual(self.gh("label", "create", "bug", "--color", "d73a4a", "--description", "Erro").returncode, 0)
        refused = self.gh("label", "create", "bug", "--color", "000000", "--description", "x")
        self.assertEqual(refused.returncode, 1)
        self.assertIn("already exists", refused.stderr)
        self.assertEqual(self.gh("label", "create", "bug", "--color", "000000", "--description", "x", "--force").returncode, 0)
        listed = json.loads(self.gh("label", "list", "--repo", REPOSITORY, "--limit", "300", "--json", "name").stdout)
        self.assertEqual(listed, [{"name": "bug"}])
        self.assertEqual(self.state()["labels"]["bug"], {"color": "000000", "description": "x"})

    def test_issues_are_viewed_listed_edited_and_closed(self):
        view = json.loads(self.gh("issue", "view", "2", "--repo", REPOSITORY, "--json", "state,labels").stdout)
        self.assertEqual(view, {"state": "CLOSED", "labels": [{"name": "bug"}]})
        self.assertEqual(self.gh("issue", "view", "99", "--repo", REPOSITORY, "--json", "state").returncode, 1)
        listed = json.loads(self.gh("issue", "list", "--label", "bug", "--state", "all", "--json", "number").stdout)
        self.assertEqual(listed, [{"number": 2}])
        self.assertEqual(json.loads(self.gh("issue", "list", "--label", "bug", "--json", "number").stdout), [])

        self.assertEqual(self.gh("issue", "edit", "1", "--repo", REPOSITORY, "--add-sub-issue", "2").returncode, 0)
        body = Path(self._tmp.name) / "body.md"
        body.write_bytes(b"## Itens\n- [x] #2\n")
        self.assertEqual(self.gh("issue", "edit", "1", "--repo", REPOSITORY, "--body-file", str(body)).returncode, 0)
        state = self.state()
        self.assertEqual((state["issues"]["1"]["sub_issues"], state["issues"]["2"]["parent"]), ([2], 1))
        self.assertEqual(state["issues"]["1"]["body"], "## Itens\n- [x] #2\n")

        graphql = 'query { repository(owner: "acme", name: "demo") { i1: issueOrPullRequest(number: 1) { x } ' \
            "i10: issueOrPullRequest(number: 10) { x } i99: issueOrPullRequest(number: 99) { x } } }"
        completed = self.gh("api", "graphql", "-f", f"query={graphql}")
        self.assertEqual(completed.returncode, 1)  # como o gh: um número inexistente dá erro, os outros vêm
        nodes = json.loads(completed.stdout)["data"]["repository"]
        self.assertEqual(nodes["i1"]["subIssues"]["nodes"], [{"number": 2, "repository": {"nameWithOwner": "acme/demo"}}])
        self.assertEqual((nodes["i10"], nodes["i99"]), ({"__typename": "PullRequest"}, None))

        closed = self.gh("issue", "close", "1", "--repo", REPOSITORY, "--reason", "not planned", "--comment", "fim")
        self.assertEqual(closed.returncode, 0)
        view = json.loads(self.gh("issue", "view", "1", "--json", "state,stateReason").stdout)
        self.assertEqual(view, {"state": "CLOSED", "stateReason": "NOT_PLANNED"})
        self.assertEqual(self.state()["comments"]["1"][-1]["body"], "fim")

    def test_an_unknown_command_still_fails_loudly(self):
        completed = self.gh("pr", "merge", "10")
        self.assertEqual(completed.returncode, 2)
        self.assertIn("comando não simulado", completed.stderr)


if __name__ == "__main__":
    unittest.main()
