"""Lacunas da issue #34 nos comandos: ``start``, ``sync-labels``, ``status``, ``bump`` e o modo offline.

``start`` também muda na #30 (slug cortado em palavra, arquivos não rastreados, branch que já
existe): aqui só entra o que sobrevive a essa mudança, como o prefixo e o número da branch.
"""

import json
import unittest

import support  # noqa: F401  (põe scripts/ no sys.path antes de importar devflow)
from devflow.config import Config
from support import BASE_CONFIG, SandboxCase

FIX = [("fix: handle the empty list", None)]
FEAT = [("feat: add the export command", None)]
OFFLINE = {"DEV_WORKFLOW_OFFLINE": "1"}


class StartTest(SandboxCase):
    def setUp(self):
        super().setUp()
        self.box.issue(42, "Corrigir a origem pública do convite", "bug")
        self.box.issue(43, "Exportar em CSV", "feature")
        self.box.issue(44, "Sem tipo")

    def test_dry_run_prints_the_branch_and_changes_nothing(self):
        box = self.box
        out = self.assertRan(box.run("github_issues.py", "start", "42", "--dry-run")).strip()
        self.assertTrue(out.startswith("fix/42-corrigir-"), out)
        self.assertEqual(len(out.splitlines()), 1)
        self.assertEqual(box.git("branch", "--show-current"), "main")
        self.assertNotIn("fix/42", box.git("branch", "--list"))
        self.assertTrue(self.assertRan(box.run("github_issues.py", "start", "#43", "--dry-run")).startswith("feat/43-"))

    def test_start_creates_the_branch_from_the_remote_default_branch(self):
        box = self.box
        box.git("commit", "-q", "--allow-empty", "-m", "chore: local only")  # não vai para a branch nova
        out = self.assertRan(box.run("github_issues.py", "start", "43"))
        branch = box.git("branch", "--show-current")
        self.assertTrue(branch.startswith("feat/43-exportar"), branch)
        self.assertIn(f"Branch {branch} criada a partir de origin/main.", out)
        self.assertEqual(box.git("rev-parse", "HEAD"), box.git("rev-parse", "origin/main"))

    def test_the_prefix_follows_the_configured_type(self):
        box = self.box
        config = BASE_CONFIG + '\n[types.bug]\nprefixes = ["bugfix", "fix"]\n[types.feature]\nprefixes = ["feat"]\n'
        box.write({".github/dev-workflow.toml": config})
        box.git("commit", "-q", "-am", "chore: rename the bug prefix")
        out = self.assertRan(box.run("github_issues.py", "start", "42", "--dry-run"))
        self.assertTrue(out.startswith("bugfix/42-"), out)

    def test_an_issue_without_exactly_one_type_is_refused(self):
        completed = self.box.run("github_issues.py", "start", "44", "--dry-run")
        self.assertEqual(completed.returncode, 1)
        self.assertIn("#44 precisa de exatamente um label de tipo", completed.stderr)
        self.box.issue(45, "Dois tipos", "bug", "feature")
        self.assertEqual(self.box.run("github_issues.py", "start", "45", "--dry-run").returncode, 1)

    def test_a_pr_or_a_missing_number_is_not_an_issue(self):
        box = self.box
        box.open_pr(11, "feat: add the export command", FEAT)
        for number in ("11", "99"):
            completed = box.run("github_issues.py", "start", number, "--dry-run")
            self.assertEqual(completed.returncode, 1)
            self.assertIn(f"#{number} não é uma issue de acme/demo.", completed.stderr)

    def test_a_modified_tracked_file_stops_the_switch(self):
        box = self.box
        box.write({"api/client.py": "def fetch():\n    return 2\n"})
        completed = box.run("github_issues.py", "start", "42")
        self.assertEqual(completed.returncode, 1)
        self.assertIn("A árvore de trabalho tem mudanças", completed.stderr)
        self.assertEqual(box.git("branch", "--show-current"), "main")

    def test_a_number_is_required(self):
        for args in (("start",), ("start", "abc")):
            completed = self.box.run("github_issues.py", *args)
            self.assertEqual(completed.returncode, 2)
            self.assertIn("usage: github_issues.py start", completed.stderr)


class SyncLabelsTest(SandboxCase):
    def test_every_label_of_the_flow_is_created_with_its_color_and_description(self):
        box = self.box
        out = self.assertRan(box.run("github_issues.py", "sync-labels"))
        expected = Config().all_labels()
        labels = box.state()["labels"]
        self.assertEqual(sorted(labels), sorted(expected))
        for name, (color, description) in expected.items():
            self.assertEqual(labels[name], {"color": color, "description": description}, name)
            self.assertIn(f"label {name} ok", out)
        self.assertIn("release:rc", labels)

        self.assertRan(box.run("github_issues.py", "sync-labels"))  # de novo: `--force` atualiza
        self.assertEqual(box.state()["labels"], labels)

    def test_a_label_that_fails_is_reported_and_the_others_still_go(self):
        box = self.box
        box.update_state(lambda state: state.__setitem__("fail_labels", ["bug"]))
        completed = box.run("github_issues.py", "sync-labels")
        self.assertEqual(completed.returncode, 1)
        self.assertIn("label bug ERRO: HTTP 403", completed.stdout)
        self.assertIn("label feature ok", completed.stdout)
        self.assertNotIn("bug", box.state()["labels"])
        self.assertIn("release", box.state()["labels"])

    def test_without_versioning_only_the_type_and_epic_labels_exist(self):
        box = self.box
        box.write({".github/dev-workflow.toml": BASE_CONFIG.replace("[versioning]\n", "[versioning]\nenabled = false\n")})
        self.assertRan(box.run("github_issues.py", "sync-labels"))
        labels = box.state()["labels"]
        self.assertIn("epic", labels)
        self.assertFalse({"release", "breaking-change", "semver:major", "release:rc"} & set(labels))

    def test_doctor_reads_the_labels_through_gh_label_list(self):
        box = self.box
        self.assertRan(box.run("github_issues.py", "sync-labels"))
        box.update_state(lambda state: state["labels"].pop("epic"))
        out = box.run("bootstrap.py", "doctor").stdout
        self.assertIn("labels ausentes: epic (rode `github_issues.py sync-labels`)", out)


class StatusTest(SandboxCase):
    def test_nothing_accumulated(self):
        out = self.assertRan(self.box.run("release.py", "status"))
        self.assertIn("Nada acumulado desde v1.2.0.", out)

    def test_status_shows_what_would_be_released_and_who_asked(self):
        box = self.box
        box.merge_pr(10, "feat: add the export command", FEAT)
        out = self.assertRan(box.run("release.py", "status"))
        self.assertIn("### Próxima versão: **v1.3.0** (minor) — seria publicada (nenhum PR pediu versão ainda)", out)
        box.merge_pr(11, "fix: handle the empty list", FIX, labels=["release"])
        before = box.state()
        out = self.assertRan(box.run("release.py", "status"))
        self.assertIn("### Próxima versão: **v1.3.0** (minor) — será publicada", out)
        self.assertIn("Versão pedida por: #11.", out)
        after = box.state()
        self.assertEqual((after["comments"], after["releases"], box.tags()), (before["comments"], {}, ["v1.2.0"]))

    def test_the_json_status_and_an_open_pr_counted_as_merged(self):
        box = self.box
        box.merge_pr(10, "fix: handle the empty list", FIX)
        branch = box.open_pr(11, "feat: add the export command", FEAT, labels=["release"])
        data = json.loads(self.assertRan(box.run("release.py", "status", "--format", "json")))
        self.assertEqual((data["base_tag"], data["next_version"], data["level"]), ("v1.2.0", "1.2.1", "patch"))
        self.assertEqual([change["key"] for change in data["changes"]], ["#10"])

        args = ("release.py", "status", "--format", "json", "--pr", "11", "--base-ref", "main", "--head-ref", branch)
        data = json.loads(self.assertRan(box.run(*args)))
        self.assertEqual((data["next_tag"], data["requested"]), ("v1.3.0", True))
        pending = [change for change in data["changes"] if change["pending"]]
        self.assertEqual([change["key"] for change in pending], ["#11"])


class BumpTest(SandboxCase):
    def test_dry_run_lists_the_files_and_writes_nothing(self):
        box = self.box
        out = self.assertRan(box.run("release.py", "bump", "--to", "1.4.0", "--dry-run"))
        self.assertEqual(out.split(), ["pyproject.toml", "package.json"])
        self.assertEqual(box.git("status", "--porcelain"), "")

    def test_bump_writes_every_declared_file(self):
        box = self.box
        self.assertRan(box.run("release.py", "bump", "--to", "2.0.0-rc.1"))
        self.assertIn('version = "2.0.0rc1"', box.read("pyproject.toml"))
        self.assertIn('"version": "2.0.0-rc.1"', box.read("package.json"))
        # a tag continua em v1.2.0: o check aponta a diferença
        self.assertEqual(box.run("release.py", "check").returncode, 1)

    def test_bumping_to_the_current_version_changes_nothing(self):
        self.assertIn("Nenhum arquivo mudou.", self.assertRan(self.box.run("release.py", "bump", "--to", "1.2.0")))

    def test_an_invalid_version_or_drifted_files_are_errors(self):
        box = self.box
        completed = box.run("release.py", "bump", "--to", "1.4")
        self.assertEqual(completed.returncode, 1)
        self.assertIn("ERRO:", completed.stderr)
        self.assertRan(box.run("release.py", "bump", "--to", "1.3.0"))
        drifted = box.run("release.py", "bump", "--to", "1.4.0")  # parte da tag (1.2.0), não dos arquivos
        self.assertEqual(drifted.returncode, 1)
        self.assertIn("ERRO:", drifted.stderr)
        self.assertIn('"version": "1.3.0"', box.read("package.json"))


class OfflineTest(SandboxCase):
    """``DEV_WORKFLOW_OFFLINE=1``: só o histórico do git, e nenhuma chamada ao ``gh``."""

    def assertNoGitHub(self):
        state = self.box.state()
        self.assertEqual((state.get("log", []), state.get("cli", [])), ([], []))

    def test_run_accumulates_without_labels_and_force_publishes_only_the_tag(self):
        box = self.box
        box.issue(1, "Corrigir a lista vazia", "bug")
        box.merge_pr(10, "fix: handle the empty list", FIX, labels=["release"], closes=[1])
        out = self.assertRan(box.release(env=OFFLINE))
        self.assertIn("Nada a versionar: 1 mudança(s)", out)  # o label só existe na API
        self.assertEqual(box.tags(), ["v1.2.0"])

        out = self.assertRan(box.release("--force", env=OFFLINE))
        self.assertIn("Tag v1.2.1 criada", out)
        self.assertEqual(box.remote_tags(), ["v1.2.0", "v1.2.1"])
        self.assertEqual(box.log("-1"), ["chore(release): v1.2.1"])
        changelog = box.read("CHANGELOG.md")
        self.assertIn("## [1.2.1]", changelog)
        self.assertIn("#1", changelog)  # a referência fica, sem título nem link da API
        self.assertNoGitHub()
        self.assertEqual(box.state()["releases"], {})

    def test_status_check_next_and_preview_work_from_the_history(self):
        box = self.box
        box.merge_pr(10, "feat: add the export command", FEAT)
        branch = box.open_pr(11, "fix: handle the empty list", FIX, labels=["release"])
        data = json.loads(self.assertRan(box.run("release.py", "status", "--format", "json", env=OFFLINE)))
        self.assertEqual((data["next_version"], data["requested"]), ("1.3.0", False))
        self.assertIn("coerentes com a versão 1.2.0", self.assertRan(box.run("release.py", "check", env=OFFLINE)))
        self.assertEqual(self.assertRan(box.run("release.py", "next", env=OFFLINE)).strip(), "1.3.0")
        args = ("release.py", "preview", "--pr", "11", "--base-ref", "main", "--head-ref", branch, "--comment")
        out = self.assertRan(box.run(*args, env=OFFLINE))
        self.assertIn("este PR não publica versão", out)  # sem a API, o label do PR não é visto
        self.assertNoGitHub()

    def test_record_analysis_prints_the_comment_instead_of_posting(self):
        box = self.box
        path = box.base / "analysis.json"
        path.write_text(
            json.dumps({"schema": 1, "base": "v1.2.0", "head": "abc1234", "changes": [
                {"pr": 10, "level": "minor", "summary": "nova opção", "evidence": []}
            ]}),
            encoding="utf-8",
        )
        out = self.assertRan(box.run("release.py", "record-analysis", "--pr", "10", "--file", str(path), env=OFFLINE))
        self.assertIn("<!-- dev-workflow:analysis", out)
        self.assertNoGitHub()

    def test_the_issue_commands_that_need_github_stop(self):
        box = self.box
        box.issue(42, "Corrigir a origem pública do convite", "bug")
        refused = box.run("github_issues.py", "check-pr", "--pr", "10", env=OFFLINE)
        self.assertEqual(refused.returncode, 2)
        self.assertIn("uso: github_issues.py check-pr", refused.stderr)
        # Sem a API a issue não é lida: a mensagem diz isso, e não que a issue não existe (#56).
        refused = box.run("github_issues.py", "start", "42", "--dry-run", env=OFFLINE)
        self.assertEqual(refused.returncode, 1)
        self.assertIn("start precisa do GitHub", refused.stderr)
        self.assertIn("DEV_WORKFLOW_OFFLINE", refused.stderr)
        self.assertNotIn("não é uma issue", refused.stderr)
        self.assertNoGitHub()

    def test_the_commit_msg_hook_never_needs_github(self):
        box = self.box
        box.git("switch", "-q", "-c", "fix/42-x")
        path = box.base / "COMMIT_EDITMSG"
        path.write_text("fix: x\n", encoding="utf-8")
        self.assertRan(box.run("github_issues.py", "commit-msg", str(path), env=OFFLINE))
        self.assertIn("Refs: #42", path.read_text(encoding="utf-8"))
        self.assertNoGitHub()

    # Defeito encontrado (não corrigido aqui, regra da #34): `sync-labels` ignora o modo offline e
    # chama `gh label create` mesmo com DEV_WORKFLOW_OFFLINE=1, ao contrário de `bootstrap.py init`,
    # que pula os labels offline. O teste documenta o comportamento atual.
    def test_sync_labels_does_not_call_gh_when_offline(self):
        box = self.box
        refused = box.run("github_issues.py", "sync-labels", env=OFFLINE)
        self.assertEqual(refused.returncode, 1)
        self.assertIn("DEV_WORKFLOW_OFFLINE", refused.stderr)
        self.assertNotIn("cli", box.state())  # nenhuma chamada ao `gh` (#55)

    # No modo pull-request, offline, a branch de release é enviada e a saída diz que o PR não foi
    # aberto e como abri-lo; antes dizia "PR de release #? aberto" (#54).
    def test_pull_request_mode_offline_does_not_claim_to_open_a_pr(self):
        box = self.box
        box.write({".github/dev-workflow.toml": BASE_CONFIG + '\n[release]\nmode = "pull-request"\n'})
        box.git("commit", "-q", "-am", "chore: release by pull request")
        box.git("push", "-q", "origin", "main")
        box.merge_pr(10, "fix: handle the empty list", FIX)
        out = self.assertRan(box.release("--force", env=OFFLINE))
        self.assertNotIn("PR de release #? aberto", out)
        self.assertIn("o PR de release não foi aberto", out)
        self.assertIn("gh pr create --base main --head release/next", out)
        self.assertEqual(box.git("ls-remote", "--heads", "origin", "release/next").count("release/next"), 1)


if __name__ == "__main__":
    unittest.main()
