"""Verificação do PR, evidências no merge, hook commit-msg, branch da issue e bootstrap."""

import subprocess
import unittest

import support  # noqa: F401  (põe scripts/ no sys.path antes de importar devflow)
from devflow.config import Config
from devflow.issues import branch_name, check_pull_request
from support import PR_BODY, SCRIPTS, Sandbox, SandboxCase

CFG = Config()
ISSUE_BODY = "\n\n".join(f"## {name}\nconteúdo" for name in CFG.issue_sections("feature")).replace(
    "## Critérios de aceite\nconteúdo", "## Critérios de aceite\n- [x] exporta\n- [x] recusa vazio"
)


def _pr(title="feat: add the export command", labels=("release",), body=None, author="alice", head="feat/1-export"):
    return {
        "number": 9,
        "title": title,
        "body": PR_BODY.format(issues="Closes #1", summary="x", version="Funcionalidade nova")
        if body is None
        else body,
        "labels": [{"name": name} for name in labels],
        "user": {"login": author},
        "head": {"ref": head},
    }


def _fetch(**issues):
    table = {1: {"labels": [{"name": "feature"}], "body": ISSUE_BODY}, **{int(k[1:]): v for k, v in issues.items()}}
    return table.get


def _check(pr, fetch=None, cfg=CFG):
    return check_pull_request(pr, "acme/demo", fetch or _fetch(), cfg)


class CheckPullRequestTest(unittest.TestCase):
    def test_a_complete_pr_passes_without_warnings(self):
        findings = _check(_pr())
        self.assertEqual((findings.errors, findings.warnings), ([], []))

    def test_exactly_one_of_release_and_no_release_is_required(self):
        for labels in ((), ("release", "no-release")):
            errors = _check(_pr(labels=labels)).errors
            self.assertEqual(len(errors), 1, labels)
            self.assertIn("exatamente um label: `release`", errors[0])
            self.assertIn("`no-release` (fica acumulado", errors[0])
        self.assertEqual(_check(_pr(labels=("no-release",))).errors, [])

    def test_template_sections_must_be_present_and_filled(self):
        empty = PR_BODY.format(issues="Closes #1", summary="x", version="<!-- só a dica do modelo -->\n-")
        self.assertIn("A seção `## Versão` está vazia", _check(_pr(body=empty)).errors[0])
        missing = PR_BODY.format(issues="Closes #1", summary="x", version="ok").replace(
            "## Testes executados", "## Outra"
        )
        self.assertIn("Falta a seção `## Testes executados`", _check(_pr(body=missing)).errors[0])

    def test_an_override_needs_the_release_label_and_a_reason(self):
        with_reason = PR_BODY.format(
            issues="Closes #1", summary="x", version="Motivo da sobreposição: só muda o padrão"
        )
        self.assertEqual(_check(_pr(labels=("release", "semver:patch"), body=with_reason)).errors, [])
        self.assertIn("sem motivo", _check(_pr(labels=("release", "semver:patch"))).errors[0])
        self.assertIn(
            "só vale em PR marcado", _check(_pr(labels=("no-release", "semver:patch"), body=with_reason)).errors[0]
        )
        self.assertIn(
            "Mais de um label",
            _check(_pr(labels=("release", "semver:patch", "semver:major"), body=with_reason)).errors[0],
        )
        self.assertIn("não é major", _check(_pr(labels=("release", "semver:huge"), body=with_reason)).errors[0])

    def test_the_issue_link_is_still_required(self):
        body = PR_BODY.format(issues="Sem issue", summary="x", version="ok")
        self.assertIn("não referencia issue", _check(_pr(body=body)).errors[0])
        self.assertIn("exatamente um label de tipo", _check(_pr(), _fetch(n1={"labels": []})).errors[0])

    def test_the_title_must_be_a_conventional_commit(self):
        self.assertIn("Conventional Commits", _check(_pr(title="Add export")).errors[0])

    def test_soft_rules_warn_and_can_be_made_errors(self):
        pending = {"labels": [{"name": "feature"}], "body": ISSUE_BODY.replace("- [x] recusa", "- [ ] recusa")}
        findings = _check(_pr(), _fetch(n1=pending))
        self.assertEqual(findings.errors, [])
        self.assertIn("1 de 2 critérios de aceite sem marcar", findings.warnings[0])
        strict = Config({"pr": {"severity": {"unchecked_criteria": "error"}}})
        self.assertIn("critérios de aceite", _check(_pr(), _fetch(n1=pending), strict).errors[0])

        thin = {"labels": [{"name": "feature"}], "body": "## Contexto\nsó isto"}
        self.assertIn("sem as seções do modelo: Objetivo", _check(_pr(), _fetch(n1=thin)).warnings[0])
        self.assertIn("O título usa `fix`", _check(_pr(title="fix: export")).warnings[0])

    def test_bots_and_the_release_pr_are_exempt(self):
        for pr in (_pr(author="dependabot[bot]", body="", labels=()), _pr(head="release/next", body="", labels=())):
            findings = _check(pr)
            self.assertEqual((findings.errors, len(findings.notes)), ([], 1))

    def test_without_versioning_no_classification_is_asked(self):
        cfg = Config({"versioning": {"enabled": False}})
        self.assertEqual(_check(_pr(labels=()), cfg=cfg).errors, [])

    def test_branch_name_comes_from_the_issue(self):
        issue = {"number": 42, "title": "Corrigir a origem pública do convite", "labels": [{"name": "bug"}]}
        self.assertEqual(branch_name(issue, CFG), "fix/42-corrigir-a-origem-publica-do-convite")
        with self.assertRaises(ValueError):
            branch_name({"number": 1, "title": "x", "labels": []}, CFG)


class MergedPullRequestTest(SandboxCase):
    def _merge(self, labels=("release",)):
        box = self.box
        box.issue(1, "Exportar em CSV", "feature", body="## Critérios de aceite\n- [x] exporta\n- [ ] recusa vazio\n")
        box.issue(2, "Documentar a exportação", "documentation")
        box.merge_pr(
            10,
            "feat: add the export command",
            [("feat: add the export command", None)],
            labels=labels,
            closes=[1],
            refs=[2],
        )
        return box

    def test_each_issue_gets_the_evidence_once(self):
        box = self._merge()
        for _ in range(2):
            self.assertRan(box.run("github_issues.py", "pr-merged", "--pr", "10"))
        state = box.state()
        closing, partial = state["comments"]["1"], state["comments"]["2"]
        self.assertEqual((len(closing), len(partial)), (1, 1))
        body = closing[0]["body"]
        self.assertIn("**Entregue pelo PR #10**, mesclado por @alice.", body)
        self.assertIn("feat: add the export command", body)
        self.assertIn("- `make test` — 12 passaram", body)
        self.assertIn("1 de 2 sem marcar no momento do merge", body)
        self.assertIn("- [ ] recusa vazio", body)
        self.assertIn("o PR está marcado para versionar", body)
        self.assertIn("**Entrega parcial pelo PR #10**", partial[0]["body"])

    def test_a_marker_from_an_untrusted_author_does_not_suppress_the_evidence(self):
        box = self._merge()
        box.comment(1, "<!-- dev-workflow:evidence pr=10 -->", login="mallory", association="NONE")
        self.assertRan(box.run("github_issues.py", "pr-merged", "--pr", "10"))
        self.assertEqual(len(box.state()["comments"]["1"]), 2)

    def test_an_unclassified_merge_becomes_no_release(self):
        box = self._merge(labels=())
        self.assertRan(box.run("github_issues.py", "pr-merged", "--pr", "10"))
        state = box.state()
        self.assertEqual([label["name"] for label in state["pulls"]["10"]["labels"]], ["no-release"])
        self.assertIn("mesclado sem classificação", state["comments"]["10"][0]["body"])
        self.assertIn("fica acumulada", state["comments"]["1"][0]["body"])
        self.assertIn("Nada a versionar: 1 mudança(s)", self.assertRan(box.release()))

    def test_check_pr_reads_the_pr_from_github(self):
        box = self._merge(labels=())
        completed = box.run("github_issues.py", "check-pr", "--pr", "10")
        self.assertEqual(completed.returncode, 1)
        self.assertIn("exatamente um label", completed.stdout)


class CommitMsgHookTest(SandboxCase):
    def _run(self, message, branch=None):
        box = self.box
        if branch:
            box.git("switch", "-q", "-c", branch)
        path = box.base / "COMMIT_EDITMSG"
        path.write_text(message, encoding="utf-8")
        completed = box.run("github_issues.py", "commit-msg", str(path))
        return completed, path.read_text(encoding="utf-8")

    def test_a_reference_passes_and_its_absence_is_refused(self):
        self.assertEqual(self._run("feat: x\n\nRefs #12\n")[0].returncode, 0)
        refused, _ = self._run("feat: x\n")
        self.assertEqual(refused.returncode, 1)
        self.assertIn("precisa referenciar uma issue", refused.stderr)

    def test_the_branch_number_becomes_the_trailer(self):
        completed, message = self._run("feat: x\n", branch="feat/42-export")
        self.assertEqual(completed.returncode, 0)
        self.assertIn("Refs: #42", message)

    def test_generated_commits_and_the_release_commit_pass(self):
        for message in ("Merge branch 'main' into x\n", 'Revert "feat: x"\n', "chore(release): v1.2.1\n\nnotas\n"):
            self.assertEqual(self._run(message)[0].returncode, 0, message)


class BootstrapTest(unittest.TestCase):
    def test_init_in_an_empty_project_installs_everything_and_doctor_passes(self):
        box = Sandbox(
            config="", files={"package.json": '{\n  "name": "demo",\n  "version": "2.4.1"\n}\n', "VERSION": "2.4.1\n"}
        )
        self.addCleanup(box.close)
        (box.work / ".github" / "dev-workflow.toml").unlink()
        box.git("commit", "-q", "-am", "chore: drop the config")
        completed = box.run("bootstrap.py", "init", "--no-labels")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("skill copiado para .claude/skills/dev-workflow", completed.stdout)

        config = box.read(".github/dev-workflow.toml")
        self.assertIn('initial_version = "2.4.1"', config)
        self.assertIn('path = "package.json"\nsearch = \'"version": "{current}"\'', config)
        self.assertIn("path = \"VERSION\"\nsearch = '{current}'", config)
        workflow = box.read(".github/workflows/release.yml")
        self.assertIn("python3 .claude/skills/dev-workflow/scripts/release.py run", workflow)
        self.assertNotIn("__", workflow.replace("__pycache__", ""))
        for name in ("pr-check.yml", "pr-merged.yml", "epic-checklist.yml", "semver-analysis.yml"):
            self.assertTrue((box.work / ".github/workflows" / name).is_file(), name)
        self.assertTrue((box.work / ".github/ISSUE_TEMPLATE/3-bug.yml").is_file())
        self.assertIn("## Versão", box.read(".github/pull_request_template.md"))
        self.assertIn("github_issues.py", box.read(".git/hooks/commit-msg"))

        vendored = box.work / ".claude/skills/dev-workflow/scripts"
        box.git("add", "-A")
        committed = subprocess.run(
            ["git", "commit", "-q", "-m", "chore: adopt the workflow\n\nRefs #1"],
            cwd=box.work,
            capture_output=True,
            text=True,
        )
        self.assertEqual(committed.returncode, 0, committed.stderr)
        refused = subprocess.run(
            ["git", "commit", "-q", "--allow-empty", "-m", "chore: no issue"],
            cwd=box.work,
            capture_output=True,
            text=True,
        )
        self.assertEqual(refused.returncode, 1)

        doctor = box.run(str(vendored / "bootstrap.py"), "doctor")
        self.assertEqual(doctor.returncode, 0, doctor.stdout + doctor.stderr)
        self.assertIn("2 entrada(s) de arquivo de versão coerentes com 2.4.1", doctor.stdout)
        self.assertIn("sem tag de versão", doctor.stdout)
        self.assertIn("0 erro(s)", doctor.stdout)

        again = box.run(str(vendored / "bootstrap.py"), "init", "--no-labels")
        self.assertIn(".github/dev-workflow.toml mantido (já existe)", again.stdout)
        self.assertIn("release.yml já está igual ao modelo", again.stdout)

    def test_the_scripts_do_not_depend_on_the_host_project(self):
        forbidden = (
            "import django",
            "from django",
            "import skeleton",
            "from skeleton",
            "from tools",
            "import yaml",
            "import requests",
        )
        for path in SCRIPTS.rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            for needle in forbidden:
                self.assertNotIn(needle, source, f"{path.name}: {needle}")


if __name__ == "__main__":
    unittest.main()
