"""Simulação de ponta a ponta do versionamento num repositório git temporário fora do projeto.

Cada teste monta um projeto de mentira com remoto *bare*, mescla PRs como o GitHub faria e roda
``release.py run`` de verdade, em subprocesso, contra um ``gh`` simulado. O que não é exercitado
aqui é só a camada do GitHub Actions (gatilhos, permissões e o ``gh`` real).
"""

import argparse
import os
import subprocess
import unittest
from unittest import mock

import support  # noqa: F401  (põe scripts/ no sys.path antes de importar devflow)
from devflow.config import load
from devflow.gh import Git
from devflow.release import Session, run
from support import BASE_FILES, PR_BODY, Sandbox, SandboxCase

FIX = [("fix: handle the empty list", None)]
FEAT = [("feat: add the export command", None)]


def _bodies(state, number):
    return [comment["body"] for comment in state["comments"].get(str(number), [])]


class AccumulationTest(SandboxCase):
    def test_pr_without_version_waits_and_the_next_release_pr_includes_it(self):
        box = self.box
        box.issue(1, "Corrigir a lista vazia", "bug")
        box.issue(2, "Corrigir o arredondamento", "bug")
        box.merge_pr(10, "fix: handle the empty list", FIX, closes=[1])

        out = self.assertRan(box.release())
        self.assertIn("Nada a versionar: 1 mudança(s) acumulada(s)", out)
        self.assertEqual(box.tags(), ["v1.2.0"])
        self.assertEqual(box.state()["releases"], {})

        box.merge_pr(11, "fix: round half up", [("fix: round half up", None)], labels=["release"], closes=[2])
        out = self.assertRan(box.release())
        self.assertIn("v1.2.1", out)

        # só correções acumuladas → patch, escrito nos arquivos declarados
        self.assertIn('version = "1.2.1"', box.read("pyproject.toml"))
        self.assertIn('"version": "1.2.1"', box.read("package.json"))
        # o changelog tem as duas entregas, agrupadas por tipo, com links para issue e PR
        changelog = box.read("CHANGELOG.md")
        self.assertIn("## [1.2.1](https://github.com/acme/demo/compare/v1.2.0...v1.2.1)", changelog)
        self.assertIn("### Correções", changelog)
        self.assertIn("Corrigir a lista vazia ([#1](https://github.com/acme/demo/issues/1))", changelog)
        self.assertIn("PR [#10](https://github.com/acme/demo/pull/10)", changelog)
        self.assertIn("Corrigir o arredondamento ([#2](https://github.com/acme/demo/issues/2))", changelog)
        # commit de release na branch remota, tag anotada no remoto e GitHub Release com notas
        self.assertEqual(box.log("-1"), ["chore(release): v1.2.1"])
        self.assertEqual(box.git("rev-parse", "HEAD"), box.git("rev-parse", "origin/main"))
        self.assertEqual(box.remote_tags(), ["v1.2.0", "v1.2.1"])
        self.assertEqual(box.git("cat-file", "-t", "v1.2.1"), "tag")
        self.assertIn("### Por que este número", box.git("tag", "-l", "--format=%(contents)", "v1.2.1"))
        state = box.state()
        release = state["releases"]["v1.2.1"]
        self.assertEqual((release["prerelease"], release["make_latest"]), (False, "true"))
        self.assertIn("Corrigir a lista vazia", release["body"])
        self.assertIn("#1 tem o label `bug`", release["body"])
        # os dois PRs e as duas issues ficam marcados: comentário e milestone com o nome da tag
        for number in (10, 11, 1, 2):
            self.assertEqual(len([b for b in _bodies(state, number) if "dev-workflow:released v1.2.1" in b]), 1)
        self.assertEqual(state["milestones"], [{"number": 1, "title": "v1.2.1", "state": "closed"}])
        self.assertEqual(state["pulls"]["10"]["milestone"], {"number": 1})
        self.assertEqual(state["issues"]["2"]["milestone"], {"number": 1})

    def test_running_again_after_the_release_changes_nothing(self):
        box = self.box
        box.issue(1, "Corrigir a lista vazia", "bug")
        box.merge_pr(10, "fix: handle the empty list", FIX, labels=["release"], closes=[1])
        self.assertRan(box.release())
        head, before = box.git("rev-parse", "HEAD"), box.state()

        out = self.assertRan(box.release())
        self.assertIn("Nada a versionar: 0 mudança(s)", out)
        after = box.state()
        self.assertEqual(box.git("rev-parse", "HEAD"), head)
        self.assertEqual(box.tags(), ["v1.2.0", "v1.2.1"])
        self.assertEqual(after["releases"], before["releases"])
        self.assertEqual(after["comments"], before["comments"])
        self.assertEqual(after["milestones"], before["milestones"])

    def test_dry_run_reports_and_writes_nothing(self):
        box = self.box
        box.merge_pr(10, "feat: add the export command", FEAT, labels=["release"])
        head = box.git("rev-parse", "HEAD")
        out = self.assertRan(box.release("--dry-run"))
        self.assertIn("**v1.3.0** (minor)", out)
        self.assertEqual((box.git("rev-parse", "HEAD"), box.tags()), (head, ["v1.2.0"]))
        self.assertEqual(box.git("status", "--porcelain"), "")
        self.assertEqual(box.state()["releases"], {})

    def test_unlabelled_changes_never_release_on_their_own(self):
        box = self.box
        box.merge_pr(10, "feat: add the export command", FEAT)
        box.write({"hotfix.txt": "x"})
        box.git("add", "-A")
        box.git("commit", "-q", "-m", "fix: direct push without a PR")
        box.git("push", "-q", "origin", "main")
        out = self.assertRan(box.release())
        self.assertIn("Nada a versionar: 2 mudança(s)", out)
        self.assertEqual(box.tags(), ["v1.2.0"])


class LevelTest(SandboxCase):
    def test_a_feature_among_the_accumulated_changes_makes_it_minor(self):
        box = self.box
        box.issue(1, "Exportar em CSV", "feature")
        box.issue(2, "Corrigir a lista vazia", "bug")
        box.merge_pr(10, "feat: add the export command", FEAT, closes=[1])
        box.merge_pr(11, "fix: handle the empty list", FIX, labels=["release"], closes=[2])
        self.assertRan(box.release())
        self.assertEqual(box.tags(), ["v1.2.0", "v1.3.0"])
        changelog = box.read("CHANGELOG.md")
        self.assertLess(changelog.index("### Funcionalidades"), changelog.index("### Correções"))
        self.assertIn("Exportar em CSV", changelog)

    def test_the_issue_label_counts_even_when_the_commits_say_less(self):
        box = self.box
        box.issue(1, "Exportar em CSV", "feature")
        box.merge_pr(10, "chore: wire the export", [("chore: wire the export", None)], labels=["release"], closes=[1])
        self.assertRan(box.release())
        self.assertEqual(box.tags(), ["v1.2.0", "v1.3.0"])

    def test_a_breaking_change_makes_it_major(self):
        box = self.box
        commits = [("feat!: drop the legacy client", None)]
        box.merge_pr(10, "feat!: drop the legacy client", commits)
        box.merge_pr(11, "fix: handle the empty list", FIX, labels=["release"])
        self.assertRan(box.release())
        self.assertEqual(box.tags(), ["v1.2.0", "v2.0.0"])
        changelog = box.read("CHANGELOG.md")
        self.assertIn("### ⚠ Mudanças incompatíveis", changelog)
        self.assertIn("drop the legacy client", changelog)

    def test_breaking_is_also_read_from_the_label_the_footer_and_removed_public_files(self):
        for name, kwargs, commits in (
            ("label", {"labels": ["release", "breaking-change"]}, FIX),
            (
                "rodapé",
                {"labels": ["release"]},
                [("fix: rename the flag\n\nBREAKING CHANGE: --all virou --every", None)],
            ),
            (
                "arquivo público removido",
                {"labels": ["release"]},
                [("refactor: drop the client", {"api/client.py": None})],
            ),
        ):
            with self.subTest(name):
                box = Sandbox(tag="v1.2.0")
                self.addCleanup(box.close)
                box.merge_pr(10, "fix: something", commits, **kwargs)
                self.assertEqual(box.release().returncode, 0)
                self.assertEqual(box.tags(), ["v1.2.0", "v2.0.0"])

    def test_squash_merges_and_direct_commits_are_found_too(self):
        box = self.box
        box.issue(1, "Exportar em CSV", "feature")
        box.merge_pr(10, "feat: add the export command", FEAT, closes=[1], squash=True)
        box.write({"hotfix.txt": "x"})
        box.git("add", "-A")
        box.git("commit", "-q", "-m", "fix: direct push without a PR")
        box.git("push", "-q", "origin", "main")
        box.merge_pr(11, "docs: explain the export", [("docs: explain the export", None)], labels=["release"])
        self.assertRan(box.release())
        self.assertEqual(box.tags(), ["v1.2.0", "v1.3.0"])
        changelog = box.read("CHANGELOG.md")
        self.assertIn("Exportar em CSV", changelog)
        self.assertIn("direct push without a PR ([", changelog)
        self.assertIn("### Documentação", changelog)

    def test_a_release_with_nothing_but_maintenance_still_bumps_the_patch(self):
        box = self.box
        box.merge_pr(10, "docs: fix a typo", [("docs: fix a typo", None)], labels=["release"])
        out = self.assertRan(box.release())
        self.assertIn("uma versão publicada sobe ao menos o patch", out)
        self.assertEqual(box.tags(), ["v1.2.0", "v1.2.1"])

    def test_a_version_floor_declared_in_the_code_raises_the_level(self):
        config = self.config + '\n[[versioning.floors]]\nglobs = ["api/**/*.py"]\nregex = \'since="([^"]+)"\'\n'
        box = Sandbox(config, tag="v1.2.0")
        self.addCleanup(box.close)
        commits = [("fix: annotate the method", {"api/client.py": 'def fetch():  # since="1.3.0"\n    return 1\n'})]
        box.merge_pr(10, "fix: annotate the method", commits, labels=["release"])
        completed = box.release()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("declara o piso 1.3.0: o nível sobe de patch para minor", completed.stdout)
        self.assertEqual(box.tags(), ["v1.2.0", "v1.3.0"])


class OverrideTest(SandboxCase):
    def test_the_owner_overrides_the_level_and_the_reason_is_recorded(self):
        box = self.box
        box.issue(1, "Exportar em CSV", "feature")
        body = PR_BODY.format(
            issues="Closes #1",
            summary="export",
            version="Motivo da sobreposição: a exportação já existia atrás de uma flag; aqui só muda o padrão.",
        )
        box.merge_pr(10, "feat: add the export command", FEAT, labels=["release", "semver:patch"], body=body)
        out = self.assertRan(box.release())
        self.assertIn("Nível sobreposto para **patch**", out)
        self.assertEqual(box.tags(), ["v1.2.0", "v1.2.1"])
        notes = box.state()["releases"]["v1.2.1"]["body"]
        self.assertIn("o cálculo dava **minor**", notes)
        self.assertIn("a exportação já existia atrás de uma flag", notes)
        self.assertIn("a exportação já existia", box.git("log", "-1", "--format=%b"))

    def test_a_manual_run_can_force_a_release_of_what_is_pending(self):
        box = self.box
        box.merge_pr(10, "fix: handle the empty list", FIX)
        out = self.assertRan(box.release("--force", "--level", "minor", "--reason", "alinha com o anúncio"))
        self.assertIn("alinha com o anúncio", out)
        self.assertEqual(box.tags(), ["v1.2.0", "v1.3.0"])

    def test_forcing_with_nothing_pending_is_an_error(self):
        completed = self.box.release("--force")
        self.assertEqual(completed.returncode, 1)
        self.assertIn("Nada se acumulou", completed.stdout)


class ZeroMajorTest(SandboxCase):
    tag = "v0.5.0"
    files = {
        "pyproject.toml": '[project]\nname = "demo"\nversion = "0.5.0"\n',
        "package.json": '{\n  "name": "demo",\n  "version": "0.5.0"\n}\n',
    }

    def test_in_0x_a_breaking_change_bumps_the_minor(self):
        box = self.box
        box.merge_pr(10, "feat!: drop the legacy client", [("feat!: drop the legacy client", None)], labels=["release"])
        out = self.assertRan(box.release())
        self.assertIn("Versão 0.x: a quebra de compatibilidade sobe o minor", out)
        self.assertEqual(box.tags(), ["v0.5.0", "v0.6.0"])
        self.assertIn("### ⚠ Mudanças incompatíveis", box.read("CHANGELOG.md"))

    def test_only_an_explicit_override_reaches_1_0_0(self):
        box = self.box
        body = PR_BODY.format(issues="Closes #", summary="x", version="Motivo da sobreposição: API declarada estável.")
        box.merge_pr(10, "fix: handle the empty list", FIX, labels=["release", "semver:major"], body=body)
        self.assertRan(box.release())
        self.assertEqual(box.tags(), ["v0.5.0", "v1.0.0"])


class CycleTargetTest(SandboxCase):
    """O alvo de um ciclo de pré-releases não desce; um repositório que já regrediu não é forçado."""

    tag = "v0.9.0"
    files = {
        "pyproject.toml": '[project]\nname = "demo"\nversion = "0.9.0"\n',
        "package.json": '{\n  "name": "demo",\n  "version": "0.9.0"\n}\n',
    }

    def _stable_candidate(self):
        box = self.box
        box.merge_pr(10, "fix: handle the empty list", FIX)
        self.assertRan(box.release("--level", "major", "--reason", "API declarada estável", "--prerelease", "rc"))
        self.assertEqual(box.tags(), ["v0.9.0", "v1.0.0-rc.1"])
        return box

    def test_the_final_after_an_overridden_prerelease_keeps_the_target_of_the_cycle(self):
        box = self._stable_candidate()
        box.merge_pr(11, "fix: round half up", [("fix: round half up", None)], labels=["release"])
        out = self.assertRan(box.release())
        self.assertEqual(box.tags(), ["v0.9.0", "v1.0.0", "v1.0.0-rc.1"])
        self.assertIn("O ciclo em andamento já publicou v1.0.0-rc.1", out)
        self.assertIn("**v0.9.0 → v1.0.0** — nível **major**.", out)
        self.assertIn('"version": "1.0.0"', box.read("package.json"))
        self.assertIn("coerentes com a versão 1.0.0", self.assertRan(box.run("release.py", "check")))

    def test_a_repository_that_already_regressed_is_not_forced_back_up(self):
        box = self._stable_candidate()
        # O que acontecia antes da correção: uma final abaixo do ciclo, com os arquivos nela.
        box.write({name: text.replace("0.9.0", "0.9.1") for name, text in self.files.items()})
        box.git("commit", "-q", "-am", "chore(release): v0.9.1")
        box.git("tag", "-a", "v0.9.1", "-m", "v0.9.1")
        box.git("push", "-q", "--tags", "origin", "main")
        box.update_state(lambda state: state["releases"].__setitem__("v0.9.1", {"tag_name": "v0.9.1"}))

        self.assertIn("coerentes com a versão 0.9.1", self.assertRan(box.run("release.py", "check")))
        box.merge_pr(11, "fix: round half up", [("fix: round half up", None)], labels=["release"])
        self.assertRan(box.release())
        self.assertEqual(box.tags(), ["v0.9.0", "v0.9.1", "v0.9.2", "v1.0.0-rc.1"])
        self.assertIn('"version": "0.9.2"', box.read("package.json"))


class AgentAnalysisTest(SandboxCase):
    ANALYSIS = (
        "<!-- dev-workflow:analysis\n"
        '{"schema": 1, "base": "v1.2.0", "head": "abc1234", "changes": '
        '[{"pr": 10, "level": "major", "summary": "fetch() passou a exigir um argumento", '
        '"evidence": ["api/client.py: assinatura de fetch"]}]}\n'
        "-->\nanálise"
    )
    COMMITS = [("fix: require the page size", {"api/client.py": "def fetch(size):\n    return size\n"})]

    def _seen(self, number, level='"major"', box=None):
        """A análise como o agente a grava: com o ``head`` que ela analisou, a ponta do PR."""
        box = box or self.box
        return self.ANALYSIS.replace("abc1234", box.head_of(number)).replace('"major"', level)

    def test_a_recorded_analysis_raises_the_level(self):
        box = self.box
        box.merge_pr(10, "fix: require the page size", self.COMMITS, labels=["release"])
        box.comment(10, self._seen(10))
        out = self.assertRan(box.release())
        self.assertIn("fetch() passou a exigir um argumento → major", out)
        self.assertIn("Análise por agente: cobre todas as mudanças.", out)
        self.assertEqual(box.tags(), ["v1.2.0", "v2.0.0"])

    def test_an_analysis_from_someone_without_write_access_is_ignored(self):
        box = self.box
        box.merge_pr(10, "fix: require the page size", self.COMMITS, labels=["release"])
        box.comment(10, self._seen(10), login="mallory", association="NONE")
        out = self.assertRan(box.release())
        self.assertIn("superfície pública tocada sem análise: `api/client.py`", out)
        self.assertIn("Análise por agente: ausente", out)
        self.assertEqual(box.tags(), ["v1.2.0", "v1.2.1"])

    def test_the_analysis_never_lowers_what_the_signals_say(self):
        box = self.box
        box.merge_pr(10, "feat: add the export command", FEAT, labels=["release"])
        box.comment(10, self._seen(10, '"patch"'))
        self.assertRan(box.release())
        self.assertEqual(box.tags(), ["v1.2.0", "v1.3.0"])

    def test_requiring_analysis_blocks_until_it_is_recorded(self):
        box = Sandbox(self.config.replace("[versioning]", "[versioning]\nrequire_analysis = true"), tag="v1.2.0")
        self.addCleanup(box.close)
        box.merge_pr(10, "fix: require the page size", self.COMMITS, labels=["release"])
        blocked = box.release()
        self.assertEqual(blocked.returncode, 1)
        self.assertIn("sem análise registrada: #10", blocked.stdout)
        self.assertEqual(box.tags(), ["v1.2.0"])
        box.comment(10, self._seen(10, '"patch"', box))
        self.assertEqual(box.release().returncode, 0)
        self.assertEqual(box.tags(), ["v1.2.0", "v1.2.1"])

    def test_record_analysis_posts_a_comment_the_release_then_reads(self):
        box = self.box
        box.merge_pr(10, "fix: require the page size", self.COMMITS, labels=["release"])
        box.update_state(
            lambda state: state.__setitem__("actor", {"login": "github-actions[bot]", "association": "NONE"})
        )
        path = box.base / "analysis.json"
        path.write_text(
            '{"schema": 1, "changes": [{"pr": 10, "level": "minor", "summary": "novo parâmetro opcional"}]}',
            encoding="utf-8",
        )
        self.assertRan(box.run("release.py", "record-analysis", "--pr", "10", "--file", str(path)))
        self.assertRan(box.release())
        self.assertEqual(box.tags(), ["v1.2.0", "v1.3.0"])

    def test_record_analysis_refuses_a_malformed_file(self):
        path = self.box.base / "analysis.json"
        path.write_text('{"schema": 1, "changes": [{"pr": 10, "level": "huge", "summary": "x"}]}', encoding="utf-8")
        completed = self.box.run("release.py", "record-analysis", "--pr", "10", "--file", str(path))
        self.assertEqual(completed.returncode, 1)
        self.assertIn("level deve ser um de", completed.stderr)


class RecoveryTest(SandboxCase):
    def _released(self):
        box = self.box
        box.issue(1, "Corrigir a lista vazia", "bug")
        box.merge_pr(10, "fix: handle the empty list", FIX, labels=["release"], closes=[1])
        return box

    def test_a_failure_after_the_tag_is_finished_by_the_next_run(self):
        box = self._released()
        box.update_state(lambda state: state.__setitem__("fail_release_once", True))
        failed = box.release()
        self.assertEqual(failed.returncode, 1)
        self.assertEqual(box.remote_tags(), ["v1.2.0", "v1.2.1"])
        self.assertEqual(box.state()["releases"], {})
        head = box.git("rev-parse", "HEAD")

        out = self.assertRan(box.release())
        self.assertIn("A tag v1.2.1 não tem GitHub Release; concluindo a publicação.", out)
        state = box.state()
        self.assertIn("Corrigir a lista vazia", state["releases"]["v1.2.1"]["body"])
        self.assertEqual(len(_bodies(state, 1)), 1)
        self.assertEqual(box.git("rev-parse", "HEAD"), head)
        self.assertEqual(box.tags(), ["v1.2.0", "v1.2.1"])

    def test_a_release_commit_without_a_tag_is_published_not_recomputed(self):
        box = self._released()
        self.assertRan(box.release())
        head = box.git("rev-parse", "HEAD")
        box.git("tag", "-d", "v1.2.1")
        box.git("push", "-q", "origin", ":refs/tags/v1.2.1")
        box.update_state(lambda state: state["releases"].clear())

        out = self.assertRan(box.release())
        self.assertIn("Commit de release de v1.2.1 sem tag; publicando.", out)
        self.assertEqual(box.git("rev-parse", "HEAD"), head)
        self.assertEqual(box.git("rev-list", "-n", "1", "v1.2.1"), head)
        self.assertEqual(box.remote_tags(), ["v1.2.0", "v1.2.1"])
        self.assertIn("v1.2.1", box.state()["releases"])

    def test_a_manual_prerelease_that_resumes_a_failed_one_ends_without_error(self):
        box = self.box
        box.merge_pr(10, "feat: add the export command", FEAT)
        self.assertRan(box.release("--prerelease", "beta"))
        box.merge_pr(11, "fix: handle the empty list", FIX)
        self.assertRan(box.release("--prerelease", "beta"))
        tags = ["v1.2.0", "v1.3.0-beta.1", "v1.3.0-beta.2"]
        self.assertEqual(box.remote_tags(), tags)
        # A execução caiu entre o push do commit de release e a tag: quem opera roda a mesma coisa de novo.
        box.git("tag", "-d", "v1.3.0-beta.2")
        box.git("push", "-q", "origin", ":refs/tags/v1.3.0-beta.2")
        box.update_state(lambda state: state["releases"].pop("v1.3.0-beta.2"))

        out = self.assertRan(box.release("--prerelease", "beta"))
        self.assertIn("Commit de release de v1.3.0-beta.2 sem tag; publicando.", out)
        self.assertIn("O pedido desta execução manual foi atendido por v1.3.0-beta.2", out)
        self.assertNotIn("Nada novo desde", out)
        self.assertEqual(box.remote_tags(), tags)
        self.assertIn("v1.3.0-beta.2", box.state()["releases"])

        # Sem nada a retomar, repetir o canal continua sendo um problema; promover, não.
        repeated = box.release("--prerelease", "beta")
        self.assertEqual(repeated.returncode, 1)
        self.assertIn("Nada novo desde v1.3.0-beta.2", repeated.stdout)
        self.assertEqual(box.remote_tags(), tags)

    def test_a_manual_run_after_a_tag_without_release_finishes_it_and_then_answers_what_it_asks(self):
        box = self.box
        box.merge_pr(10, "feat: add the export command", FEAT)
        box.update_state(lambda state: state.__setitem__("fail_release_once", True))
        failed = box.release("--prerelease", "beta")
        self.assertEqual(failed.returncode, 1)
        self.assertEqual((box.remote_tags(), box.state()["releases"]), (["v1.2.0", "v1.3.0-beta.1"], {}))

        # Pedir outro canal na retomada conclui a versão que parou e segue para a pedida.
        out = self.assertRan(box.release("--prerelease", "rc"))
        self.assertIn("A tag v1.3.0-beta.1 não tem GitHub Release; concluindo a publicação.", out)
        self.assertNotIn("foi atendido por", out)
        self.assertEqual(box.remote_tags(), ["v1.2.0", "v1.3.0-beta.1", "v1.3.0-rc.1"])
        self.assertEqual(sorted(box.state()["releases"]), ["v1.3.0-beta.1", "v1.3.0-rc.1"])

        # Repetir o mesmo pedido depois da falha termina sem erro; vale também para a versão final.
        box.update_state(lambda state: state.__setitem__("fail_release_once", True))
        self.assertEqual(box.release("--force").returncode, 1)
        self.assertEqual(box.remote_tags(), ["v1.2.0", "v1.3.0", "v1.3.0-beta.1", "v1.3.0-rc.1"])
        self.assertNotIn("v1.3.0", box.state()["releases"])
        out = self.assertRan(box.release("--force"))
        self.assertIn("A tag v1.3.0 não tem GitHub Release; concluindo a publicação.", out)
        self.assertIn("O pedido desta execução manual foi atendido por v1.3.0,", out)
        self.assertIn("v1.3.0", box.state()["releases"])

    def test_a_wrong_release_is_undone_and_redone_with_another_level(self):
        box = self._released()
        self.assertRan(box.release())
        box.git("tag", "-d", "v1.2.1")
        box.git("push", "-q", "origin", ":refs/tags/v1.2.1")
        box.update_state(lambda state: state["releases"].clear())
        box.git("revert", "--no-edit", "HEAD")
        box.git("push", "-q", "origin", "main")
        self.assertIn('version = "1.2.0"', box.read("pyproject.toml"))

        self.assertRan(box.release("--level", "minor", "--reason", "a correção muda o formato do arquivo"))
        self.assertEqual(box.tags(), ["v1.2.0", "v1.3.0"])
        changelog = box.read("CHANGELOG.md")
        self.assertIn("## [1.3.0]", changelog)
        self.assertNotIn("## [1.2.1]", changelog)
        self.assertIn("Corrigir a lista vazia", changelog)

    def test_two_releases_in_a_row_each_cover_only_their_own_changes(self):
        box = self._released()
        self.assertRan(box.release())
        box.issue(2, "Exportar em CSV", "feature")
        box.merge_pr(11, "feat: add the export command", FEAT, labels=["release"], closes=[2])
        self.assertRan(box.release())
        self.assertEqual(box.tags(), ["v1.2.0", "v1.2.1", "v1.3.0"])
        changelog = box.read("CHANGELOG.md")
        self.assertLess(changelog.index("## [1.3.0]"), changelog.index("## [1.2.1]"))
        newest = changelog[changelog.index("## [1.3.0]") : changelog.index("## [1.2.1]")]
        self.assertIn("Exportar em CSV", newest)
        self.assertNotIn("Corrigir a lista vazia", newest)

    def test_the_remote_branch_moving_during_the_release_makes_it_recompute(self):
        box = self._released()
        other = box.base / "other"
        subprocess.run(["git", "clone", "-q", str(box.remote), str(other)], check=True)

        def late_merge():
            for command in (
                [
                    "git",
                    "-c",
                    "user.name=Bob",
                    "-c",
                    "user.email=bob@example.com",
                    "commit",
                    "-q",
                    "--allow-empty",
                    "-m",
                    "feat: late arrival",
                ],
                ["git", "push", "-q", "origin", "main"],
            ):
                subprocess.run(command, cwd=other, check=True)

        with mock.patch.dict(os.environ, box.env(), clear=True):
            session = Session(load(box.work))
            push, raced = session.git.raw, []

            def racing(*args):
                if args[0] == "push" and not raced:
                    raced.append(True)
                    late_merge()
                return push(*args)

            session.git.raw = racing
            args = argparse.Namespace(force=False, level="", reason="", prerelease="", analysis_file="", dry_run=False)
            with mock.patch("builtins.print"):
                self.assertEqual(run(session, args), 0)
        self.assertEqual(box.tags(), ["v1.2.0", "v1.3.0"])  # o feat que chegou no meio entrou na conta
        self.assertIn("late arrival", box.read("CHANGELOG.md"))
        self.assertEqual(box.git("rev-parse", "HEAD"), box.git("rev-parse", "origin/main"))


FORGED = {name: text.replace("1.2.0", "9.9.9") for name, text in BASE_FILES.items() if name != "api/client.py"}


class ReleaseCommitOriginTest(SandboxCase):
    """Só publica o commit de release da automação: push direto ou PR de release deste repositório."""

    def _nothing_published(self, completed):
        out = self.assertRan(completed)
        self.assertIn("(v9.9.9) ignorado", out)
        self.assertNotIn("sem tag; publicando", out)
        self.assertEqual((self.box.tags(), self.box.remote_tags()), (["v1.2.0"], ["v1.2.0"]))
        self.assertEqual(self.box.state()["releases"], {})
        return out

    def test_a_release_commit_inside_an_ordinary_pr_is_not_published(self):
        box = self.box
        box.issue(1, "Corrigir a lista vazia", "bug")
        box.merge_pr(10, "fix: handle the empty list", [*FIX, ("chore(release): v9.9.9", FORGED)], closes=[1])
        out = self._nothing_published(box.release())
        self.assertIn("chegou pelo PR #10", out)
        self.assertIn("Nada a versionar", out)

    def test_a_squashed_pr_titled_like_a_release_is_not_published(self):
        box = self.box
        box.merge_pr(10, "chore(release): v9.9.9", [("fix: handle the empty list", FORGED)], squash=True)
        self.assertEqual(box.log("-1"), ["chore(release): v9.9.9 (#10)"])
        self.assertIn("chegou pelo PR #10", self._nothing_published(box.release()))

    def test_a_rebased_pr_with_a_release_commit_is_not_published(self):
        box = self.box
        box.merge_pr(10, "fix: handle the empty list", FIX)
        box.write(FORGED)
        box.git("commit", "-q", "-am", "chore(release): v9.9.9")  # o rebase põe o commit no primeiro pai
        box.git("push", "-q", "origin", "main")
        sha = box.git("rev-parse", "HEAD")
        box.update_state(lambda state: state["pulls"]["10"].update(commit_shas=[sha]))
        self.assertIn("chegou pelo PR #10", self._nothing_published(box.release()))

    def test_a_fork_branch_named_like_the_release_pr_is_not_published(self):
        box = self.box
        box.merge_pr(
            10,
            "chore(release): v9.9.9",
            [("chore(release): v9.9.9", FORGED)],
            branch="release/next",
            fork="mallory/demo",
        )
        self.assertEqual(box.log("-1"), ["Merge pull request #10 from mallory/release/next"])
        self.assertIn("chegou pelo PR #10", self._nothing_published(box.release()))

    def test_without_github_the_merge_subject_decides(self):
        box = self.box
        box.merge_pr(10, "fix: handle the empty list", [*FIX, ("chore(release): v9.9.9", FORGED)])
        offline = {"DEV_WORKFLOW_OFFLINE": "1"}
        self.assertIn("que não é do PR de release", self._nothing_published(box.release(env=offline)))

        box.merge_pr(
            11, "chore(release): v9.9.9", [("chore(release): v9.9.9", None)], branch="release/next", fork="mallory/demo"
        )
        self._nothing_published(box.release(env=offline))

    def test_without_github_the_release_pr_of_this_repository_still_publishes(self):
        box = Sandbox(mode="pull-request", tag="v1.2.0")
        self.addCleanup(box.close)
        box.merge_pr(10, "fix: handle the empty list", FIX, labels=["release"])
        self.assertRan(box.release())
        box.git("fetch", "-q", "origin", "release/next")
        box.git("merge", "-q", "--no-ff", "FETCH_HEAD", "-m", "Merge pull request #1001 from acme/release/next")
        box.git("push", "-q", "origin", "main")
        out = self.assertRan(box.release(env={"DEV_WORKFLOW_OFFLINE": "1"}))
        self.assertIn("Commit de release de v1.2.1 sem tag; publicando.", out)
        self.assertEqual(box.remote_tags(), ["v1.2.0", "v1.2.1"])


class PullRequestModeTest(SandboxCase):
    mode = "pull-request"

    def _prepare(self):
        box = self.box
        box.issue(1, "Corrigir a lista vazia", "bug")
        box.merge_pr(10, "fix: handle the empty list", FIX, labels=["release"], closes=[1])
        head = box.git("rev-parse", "HEAD")
        out = self.assertRan(box.release())
        self.assertIn("PR de release #1001 aberto para v1.2.1.", out)
        self.assertEqual((box.git("rev-parse", "HEAD"), box.tags()), (head, ["v1.2.0"]))
        pull = box.state()["pulls"]["1001"]
        self.assertEqual(
            (pull["title"], pull["head"]["ref"], pull["base"]["ref"]),
            ("chore(release): v1.2.1", "release/next", "main"),
        )
        return box

    def test_the_release_pr_is_opened_and_merging_it_publishes(self):
        box = self._prepare()
        again = self.assertRan(box.release())
        self.assertIn("PR de release #1001 atualizado para v1.2.1.", again)
        self.assertEqual(len(box.state()["pulls"]), 2)

        box.git("fetch", "-q", "origin", "release/next")
        box.git("merge", "-q", "--no-ff", "FETCH_HEAD", "-m", "Merge pull request #1001 from acme/release/next")
        box.git("push", "-q", "origin", "main")
        box.update_state(lambda state: state["pulls"]["1001"].update(state="closed", merged=True, merged_at="x"))
        out = self.assertRan(box.release())
        self.assertIn("Commit de release de v1.2.1 sem tag; publicando.", out)
        self.assertEqual(box.git("rev-list", "-n", "1", "v1.2.1"), box.git("rev-parse", "HEAD^2"))
        self.assertIn('version = "1.2.1"', box.read("pyproject.toml"))
        state = box.state()
        self.assertIn("Corrigir a lista vazia", state["releases"]["v1.2.1"]["body"])
        self.assertEqual(len(_bodies(state, 10)), 1)
        self.assertIn("Nada a versionar", self.assertRan(box.release()))

    def _merge_release_pr(self, number=1001):
        box = self.box
        box.git("fetch", "-q", "origin", "release/next")
        box.git("merge", "-q", "--no-ff", "FETCH_HEAD", "-m", f"Merge pull request #{number} from acme/release/next")
        box.git("push", "-q", "origin", "main")
        box.update_state(lambda state: state["pulls"][str(number)].update(state="closed", merged=True, merged_at="x"))

    def _open_release_pr(self):
        (pull,) = [pull for pull in self.box.state()["pulls"].values() if pull["state"] == "open"]
        return pull

    def test_a_prepared_prerelease_is_published_and_so_is_the_next_one(self):
        box = self.box
        box.merge_pr(10, "feat: add the export command", FEAT)
        out = self.assertRan(box.release("--prerelease", "rc"))
        self.assertIn("PR de release #1001 aberto para v1.3.0-rc.1.", out)
        self._merge_release_pr()
        out = self.assertRan(box.release())
        self.assertIn("Commit de release de v1.3.0-rc.1 sem tag; publicando.", out)
        self.assertEqual(box.remote_tags(), ["v1.2.0", "v1.3.0-rc.1"])
        self.assertTrue(box.state()["releases"]["v1.3.0-rc.1"]["prerelease"])

        box.merge_pr(11, "fix: handle the empty list", FIX)
        refused = box.release("--prerelease", "beta")
        self.assertEqual(refused.returncode, 1)
        self.assertIn("O canal `beta` é anterior a `rc`", refused.stdout)
        self.assertEqual([pull for pull in box.state()["pulls"].values() if pull["state"] == "open"], [])

        out = self.assertRan(box.release("--prerelease", "rc"))
        number = self._open_release_pr()["number"]
        self.assertIn(f"PR de release #{number} aberto para v1.3.0-rc.2.", out)
        self._merge_release_pr(number)
        out = self.assertRan(box.release())
        self.assertIn("Commit de release de v1.3.0-rc.2 sem tag; publicando.", out)
        self.assertEqual(box.remote_tags(), ["v1.2.0", "v1.3.0-rc.1", "v1.3.0-rc.2"])
        self.assertIn('version = "1.3.0rc2"', box.read("pyproject.toml"))
        self.assertIn("Nada a versionar", self.assertRan(box.release()))

    def test_the_release_pr_branch_itself_never_publishes(self):
        box = self._prepare()
        box.git("fetch", "-q", "origin", "release/next")
        box.git("switch", "-q", "-c", "release/next", "FETCH_HEAD")
        out = self.assertRan(box.release(env={"RELEASE_BRANCH": "release/next"}))
        self.assertIn("é a do PR de release", out)
        self.assertEqual((box.tags(), box.remote_tags()), (["v1.2.0"], ["v1.2.0"]))
        self.assertEqual(box.state()["releases"], {})

    def test_the_merged_release_pr_is_not_a_change_of_this_version_nor_of_the_next(self):
        box = self._prepare()
        self._merge_release_pr()
        self.assertRan(box.release())
        state = box.state()
        self.assertNotIn("#1001", state["releases"]["v1.2.1"]["body"])
        self.assertNotIn("#1001", box.git("tag", "-l", "--format=%(contents)", "v1.2.1"))
        self.assertNotIn("1001", state["comments"])
        # a tag fica no commit de release; o merge commit vem depois dela e não é mudança
        self.assertEqual(box.git("rev-list", "-n", "1", "v1.2.1"), box.git("rev-parse", "HEAD^2"))
        self.assertIn("Nada a versionar: 0 mudança(s)", self.assertRan(box.release()))

        box.issue(2, "Exportar em CSV", "feature")
        box.merge_pr(11, "feat: add the export command", FEAT, labels=["release"], closes=[2])
        self.assertRan(box.release())
        pull = self._open_release_pr()
        self.assertEqual(pull["title"], "chore(release): v1.3.0")
        self.assertIn("Exportar em CSV", pull["body"])
        self.assertNotIn("#1001", pull["body"])
        self.assertNotIn("Corrigir a lista vazia", pull["body"])

    def test_a_pr_merged_while_the_release_pr_was_open_waits_for_the_next_version(self):
        box = self._prepare()  # o PR de release cobre só o PR 10
        box.issue(2, "Exportar em CSV", "feature")
        box.merge_pr(11, "feat: add the export command", FEAT, closes=[2])
        self._merge_release_pr()  # mesclado sem ter sido refeito sobre o PR 11
        out = self.assertRan(box.release())
        self.assertEqual(box.tags(), ["v1.2.0", "v1.2.1"])
        self.assertNotIn("Exportar em CSV", box.read("CHANGELOG.md"))
        self.assertIn("Nada a versionar: 1 mudança(s) acumulada(s) desde v1.2.1", out)

        self.assertRan(box.release("--force"))
        pull = self._open_release_pr()
        self.assertEqual(pull["title"], "chore(release): v1.3.0")
        self.assertIn("Exportar em CSV", pull["body"])

    def test_the_pr_check_accepts_the_release_pr_and_only_it(self):
        box = self._prepare()
        tree = box.base / "pr-head"
        box.git("clone", "-q", "--branch", "release/next", str(box.remote), str(tree))
        commit = ["-c", "user.name=a", "-c", "user.email=a@example.com", "commit", "-q"]
        out = self.assertRan(box.run("release.py", "check", "--tree", str(tree)))
        self.assertIn("PR de release (v1.2.1)", out)
        self.assertIn("coerentes com a versão 1.2.1", out)

        # um commit de release que não traz a versão nos arquivos continua reprovado
        (tree / "pyproject.toml").write_bytes(BASE_FILES["pyproject.toml"].encode("utf-8"))
        Git(tree)(*commit, "-am", "chore(release): v1.2.1")
        self.assertEqual(box.run("release.py", "check", "--tree", str(tree)).returncode, 1)

        # um PR comum com a versão nova nos arquivos não se passa por release: a ponta não é commit de release
        Git(tree)("reset", "-q", "--hard", "HEAD~1")
        Git(tree)(*commit, "--allow-empty", "-m", "fix: other")
        self.assertEqual(box.run("release.py", "check", "--tree", str(tree)).returncode, 1)

    def test_notes_longer_than_github_accepts_are_shortened_but_the_changelog_keeps_everything(self):
        box = self.box
        padding = " detalhe" * 500  # ~4 000 caracteres por entrada: 35 entradas passam dos dois limites
        for number in range(1, 36):
            box.issue(number, f"Corrigir o caso {number}{padding}", "bug")
            labels = ["release"] if number == 35 else []
            box.merge_pr(
                100 + number, f"fix: case {number}", [(f"fix: case {number}", None)], labels=labels, closes=[number]
            )

        out = self.assertRan(box.release())
        self.assertIn("PR de release #1001 aberto para v1.2.1.", out)
        body = box.state()["pulls"]["1001"]["body"]
        self.assertLessEqual(len(body), 65_536)
        self.assertIn("dev-workflow:release-pr", body)
        self.assertIn("não cabe aqui", body)
        self.assertNotIn("| Mudança | Nível | Sinais |", body)
        self.assertIn(
            "a lista completa está no [CHANGELOG.md](https://github.com/acme/demo/blob/release/next/CHANGELOG.md)", body
        )

        box.git("fetch", "-q", "origin", "release/next")
        box.git("merge", "-q", "--no-ff", "FETCH_HEAD", "-m", "Merge pull request #1001 from acme/release/next")
        box.git("push", "-q", "origin", "main")
        box.update_state(lambda state: state["pulls"]["1001"].update(state="closed", merged=True, merged_at="x"))
        self.assertRan(box.release())
        release = box.state()["releases"]["v1.2.1"]["body"]
        self.assertLessEqual(len(release), 125_000)
        self.assertIn("https://github.com/acme/demo/blob/v1.2.1/CHANGELOG.md", release)
        changelog = box.read("CHANGELOG.md")
        self.assertTrue(all(f"Corrigir o caso {number} " in changelog for number in range(1, 36)))
        annotation = box.git("tag", "-l", "--format=%(contents)", "v1.2.1")
        self.assertIn("| Mudança | Nível | Sinais |", annotation)
        self.assertIn("Corrigir o caso 35 ", annotation)

    def test_a_squashed_release_pr_publishes_too(self):
        box = self._prepare()
        box.git("fetch", "-q", "origin", "release/next")
        box.git("merge", "-q", "--squash", "FETCH_HEAD")
        box.git("commit", "-q", "-m", "chore(release): v1.2.1 (#1001)")
        box.git("push", "-q", "origin", "main")
        box.update_state(lambda state: state["pulls"]["1001"].update(state="closed", merged=True, merged_at="x"))
        self.assertRan(box.release())
        self.assertEqual(box.tags(), ["v1.2.0", "v1.2.1"])
        self.assertEqual(box.git("rev-list", "-n", "1", "v1.2.1"), box.git("rev-parse", "HEAD"))


class PrereleaseAndMaintenanceTest(SandboxCase):
    def test_prereleases_count_up_and_the_final_release_replaces_them(self):
        box = self.box
        box.issue(1, "Exportar em CSV", "feature")
        box.merge_pr(10, "feat: add the export command", FEAT, closes=[1])
        self.assertRan(box.release("--force", "--prerelease", "rc"))
        self.assertEqual(box.tags(), ["v1.2.0", "v1.3.0-rc.1"])
        self.assertIn('version = "1.3.0rc1"', box.read("pyproject.toml"))
        self.assertIn('"version": "1.3.0-rc.1"', box.read("package.json"))
        self.assertFalse((box.work / "CHANGELOG.md").exists())
        release = box.state()["releases"]["v1.3.0-rc.1"]
        self.assertEqual((release["prerelease"], release["make_latest"]), (True, "false"))
        self.assertEqual(box.state()["comments"], {})

        box.merge_pr(11, "fix: handle the empty list", FIX)
        self.assertRan(box.release("--force", "--prerelease", "rc"))
        self.assertEqual(box.tags(), ["v1.2.0", "v1.3.0-rc.1", "v1.3.0-rc.2"])

        box.merge_pr(12, "docs: explain the export", [("docs: explain the export", None)], labels=["release"])
        self.assertRan(box.release())
        self.assertEqual(box.tags(), ["v1.2.0", "v1.3.0", "v1.3.0-rc.1", "v1.3.0-rc.2"])
        self.assertIn('version = "1.3.0"', box.read("pyproject.toml"))
        changelog = box.read("CHANGELOG.md")
        self.assertIn("compare/v1.2.0...v1.3.0", changelog)
        self.assertIn("Exportar em CSV", changelog)
        self.assertEqual(len(_bodies(box.state(), 10)), 1)

    def test_a_channel_outside_the_project_list_is_refused(self):
        box = self.box
        box.merge_pr(10, "feat: add the export command", FEAT)
        refused = box.release("--force", "--prerelease", "nightly")
        self.assertEqual(refused.returncode, 1)
        self.assertIn("`nightly` fora da lista do projeto", refused.stderr)
        self.assertIn("alpha, beta, rc", refused.stderr)
        self.assertEqual((box.tags(), box.state()["releases"]), (["v1.2.0"], {}))

        # o canal da lista sai na forma de cada arquivo: SemVer no package.json, PEP 440 no pyproject.toml
        self.assertRan(box.release("--force", "--prerelease", "BETA"))
        self.assertEqual(box.tags(), ["v1.2.0", "v1.3.0-beta.1"])
        self.assertIn('version = "1.3.0b1"', box.read("pyproject.toml"))
        self.assertIn('"version": "1.3.0-beta.1"', box.read("package.json"))

    def test_asking_for_a_channel_publishes_without_force(self):
        box = self.box
        box.merge_pr(10, "feat: add the export command", FEAT)
        self.assertRan(box.release("--prerelease", "rc"))
        self.assertEqual(box.remote_tags(), ["v1.2.0", "v1.3.0-rc.1"])
        self.assertTrue(box.state()["releases"]["v1.3.0-rc.1"]["prerelease"])

    def test_a_manual_channel_earlier_than_the_one_of_the_cycle_is_refused(self):
        box = self.box
        box.merge_pr(10, "feat: add the export command", FEAT)
        self.assertRan(box.release("--prerelease", "beta"))
        box.merge_pr(11, "fix: handle the empty list", FIX)
        self.assertRan(box.release("--prerelease", "beta"))
        tags = ["v1.2.0", "v1.3.0-beta.1", "v1.3.0-beta.2"]
        self.assertEqual(box.tags(), tags)
        head = box.git("rev-parse", "HEAD")

        refused = box.release("--prerelease", "alpha")
        self.assertEqual(refused.returncode, 1)
        self.assertIn("O canal `alpha` é anterior a `beta`", refused.stdout)
        self.assertIn("que o ciclo de 1.3.0 já publicou (v1.3.0-beta.2)", refused.stdout)
        self.assertEqual((box.tags(), box.remote_tags(), box.git("rev-parse", "HEAD")), (tags, tags, head))
        self.assertEqual(box.git("status", "--porcelain"), "")
        self.assertEqual(sorted(box.state()["releases"]), ["v1.3.0-beta.1", "v1.3.0-beta.2"])

        # promover a um canal mais maduro continua valendo, e a numeração recomeça
        self.assertRan(box.release("--prerelease", "rc"))
        self.assertEqual(box.tags(), [*tags, "v1.3.0-rc.1"])
        self.assertIn('version = "1.3.0rc1"', box.read("pyproject.toml"))

    def test_an_override_below_the_target_of_the_cycle_is_a_problem_not_a_silent_raise(self):
        box = self.box
        box.merge_pr(10, "feat!: drop the legacy client", [("feat!: drop the legacy client", None)])
        self.assertRan(box.release("--prerelease", "beta"))
        tags = ["v1.2.0", "v2.0.0-beta.1"]
        self.assertEqual(box.tags(), tags)

        body = PR_BODY.format(issues="Closes #", summary="x", version="Motivo da sobreposição: é só uma correção.")
        box.merge_pr(11, "fix: handle the empty list", FIX, labels=["release", "semver:patch"], body=body)
        refused = box.release()
        self.assertEqual(refused.returncode, 1)
        self.assertIn("A sobreposição pede 1.2.1, abaixo de 2.0.0", refused.stdout)
        self.assertIn("(v2.0.0-beta.1)", refused.stdout)
        self.assertEqual((box.tags(), box.remote_tags()), (tags, tags))

        box.update_state(lambda state: state["pulls"]["11"].update(labels=[{"name": "release"}]))
        self.assertRan(box.release())
        self.assertEqual(box.tags(), ["v1.2.0", "v2.0.0", "v2.0.0-beta.1"])

    def test_a_maintenance_line_cannot_take_a_number_the_main_cycle_is_using(self):
        box = self.box
        box.merge_pr(10, "feat: add the export command", FEAT)
        self.assertRan(box.release("--prerelease", "beta"))  # a main está no ciclo de 1.3.0
        box.git("switch", "-q", "-c", "release/1.2", "v1.2.0")
        box.git("push", "-q", "origin", "release/1.2")
        box.merge_pr(11, "feat: backport the export", FEAT, labels=["release"], base="release/1.2")
        refused = box.release()
        self.assertEqual(refused.returncode, 1)
        self.assertIn("A versão 1.3.0 já tem tag em outra linha (v1.3.0-beta.1)", refused.stdout)
        self.assertEqual(box.remote_tags(), ["v1.2.0", "v1.3.0-beta.1"])
        self.assertIn('version = "1.2.0"', box.read("pyproject.toml"))

    def test_a_maintenance_branch_releases_a_patch_that_is_not_the_latest(self):
        box = self.box
        box.merge_pr(10, "feat: add the export command", FEAT, labels=["release"])
        self.assertRan(box.release())
        box.git("switch", "-q", "-c", "release/1.2", "v1.2.0")
        box.git("push", "-q", "origin", "release/1.2")
        box.merge_pr(11, "fix: handle the empty list", FIX, labels=["release"], base="release/1.2")
        self.assertRan(box.release())
        self.assertEqual(box.tags(), ["v1.2.0", "v1.2.1", "v1.3.0"])
        self.assertEqual(box.state()["releases"]["v1.2.1"]["make_latest"], "false")
        self.assertIn('version = "1.2.1"', box.read("pyproject.toml"))

        box.merge_pr(12, "feat: backport the export", FEAT, labels=["release"], base="release/1.2")
        refused = box.release()
        self.assertEqual(refused.returncode, 1)
        self.assertIn("A tag v1.3.0 já existe", refused.stdout)

    def _merge_forward(self, line="release/1.2"):
        """Leva a linha de manutenção de volta à main, ficando com os arquivos de versão da main."""
        box = self.box
        box.git("switch", "-q", "main")
        box.git("merge", "-q", "--no-ff", "-X", "ours", line, "-m", f"Merge branch '{line}'")
        box.git("push", "-q", "origin", "main")

    def test_a_maintenance_line_merged_forward_does_not_bring_its_cycle_along(self):
        box = self.box
        box.merge_pr(10, "feat: add the export command", FEAT, labels=["release"])
        self.assertRan(box.release())
        box.git("switch", "-q", "-c", "release/1.2", "v1.2.0")
        box.git("push", "-q", "origin", "release/1.2")
        box.merge_pr(11, "fix: handle the empty list", FIX, labels=["release:rc"], base="release/1.2")
        self.assertRan(box.release())

        def main_stays_at_its_own_version():
            self.assertIn('version = "1.3.0"', box.read("pyproject.toml"))
            out = self.assertRan(box.run("release.py", "check"))
            self.assertIn("coerentes com a versão 1.3.0 (tag v1.3.0)", out)
            out = self.assertRan(box.run("release.py", "status"))
            self.assertIn("**v1.3.0 → v1.3.1**", out)
            self.assertNotIn("Ciclo de pré-releases em andamento", out)

        # Com o ciclo da linha ainda aberto: v1.2.1-rc.1 fica alcançável da main, que já publicou v1.3.0.
        self._merge_forward()
        main_stays_at_its_own_version()

        # E depois de a linha publicar a final dela, que encerra aquele ciclo.
        box.git("switch", "-q", "release/1.2")
        box.merge_pr(12, "fix: round half up", [("fix: round half up", None)], labels=["release"], base="release/1.2")
        self.assertRan(box.release())
        self.assertEqual(box.remote_tags(), ["v1.2.0", "v1.2.1", "v1.2.1-rc.1", "v1.3.0"])
        self._merge_forward()
        main_stays_at_its_own_version()

        box.merge_pr(13, "fix: keep the order", [("fix: keep the order", None)], labels=["release"])
        self.assertRan(box.release())
        self.assertEqual(box.remote_tags(), ["v1.2.0", "v1.2.1", "v1.2.1-rc.1", "v1.3.0", "v1.3.1"])
        self.assertEqual(box.state()["releases"]["v1.3.1"]["make_latest"], "true")

    def test_a_hotfix_merged_forward_during_a_cycle_leaves_the_cycle_of_the_main_line_open(self):
        box = self.box
        box.merge_pr(10, "feat: add the export command", FEAT, labels=["release:beta"])
        self.assertRan(box.release())  # a main está no ciclo de 1.3.0
        box.git("switch", "-q", "-c", "release/1.2", "v1.2.0")
        box.git("push", "-q", "origin", "release/1.2")
        box.merge_pr(11, "fix: handle the empty list", FIX, labels=["release"], base="release/1.2")
        self.assertRan(box.release())
        self.assertEqual(box.remote_tags(), ["v1.2.0", "v1.2.1", "v1.3.0-beta.1"])

        # v1.2.1 passa a ser a última final alcançável da main; o ciclo de 1.3.0 continua sendo o dela.
        self._merge_forward()
        self.assertIn("(tag v1.3.0-beta.1)", self.assertRan(box.run("release.py", "check")))
        out = self.assertRan(box.run("release.py", "status"))
        self.assertIn("Ciclo de pré-releases em andamento: v1.3.0-beta.1. Versão corrente: v1.3.0-beta.1.", out)

        box.merge_pr(12, "fix: round half up", [("fix: round half up", None)], labels=["release:beta"])
        self.assertRan(box.release())
        self.assertEqual(box.remote_tags(), ["v1.2.0", "v1.2.1", "v1.3.0-beta.1", "v1.3.0-beta.2"])
        box.merge_pr(13, "docs: explain the export", [("docs: explain the export", None)], labels=["release"])
        self.assertRan(box.release())
        self.assertIn("v1.3.0", box.remote_tags())
        self.assertIn('version = "1.3.0"', box.read("pyproject.toml"))

    def test_branches_outside_the_release_list_do_nothing(self):
        box = self.box
        box.git("switch", "-q", "-c", "feat/1-x")
        self.assertIn("não publica versões", self.assertRan(box.release("--force")))


class SurroundingsTest(SandboxCase):
    def test_a_configured_workflow_is_dispatched_with_the_new_tag(self):
        box = Sandbox(self.config + '\n[release]\ndispatch_workflow = "publish.yml"\n', tag="v1.2.0")
        self.addCleanup(box.close)
        box.merge_pr(10, "fix: handle the empty list", FIX, labels=["release"])
        self.assertEqual(box.release().returncode, 0)
        self.assertEqual(
            box.state()["dispatches"],
            [{"workflow": "publish.yml", "ref": "v1.2.1", "inputs": {"version": "1.2.1", "tag": "v1.2.1"}}],
        )

    def test_a_project_without_tags_starts_from_the_configured_version(self):
        box = Sandbox(tag=None)
        self.addCleanup(box.close)
        self.assertIn("coerentes com a versão 1.2.0", box.run("release.py", "check").stdout)
        self.assertEqual(box.run("release.py", "baseline", "--push").returncode, 0)
        self.assertEqual(box.remote_tags(), ["v1.2.0"])
        self.assertIn("já tem tags", box.run("release.py", "baseline").stdout)

    def test_files_that_drifted_from_the_current_version_stop_the_release(self):
        box = self.box
        box.merge_pr(
            10,
            "fix: bump by hand",
            [("fix: bump by hand", {"package.json": '{\n  "version": "9.9.9"\n}\n'})],
            labels=["release"],
        )
        self.assertEqual(box.run("release.py", "check").returncode, 1)
        refused = box.release()
        self.assertEqual(refused.returncode, 1)
        self.assertIn('esperava 1 ocorrência(s) de `"version": "1.2.0"`', refused.stdout)
        self.assertEqual(box.tags(), ["v1.2.0"])
        self.assertEqual(box.git("status", "--porcelain"), "")

    def test_the_preview_of_an_open_pr_is_one_comment_kept_up_to_date(self):
        box = self.box
        box.merge_pr(10, "fix: handle the empty list", FIX)
        branch = box.open_pr(11, "feat: add the export command", FEAT, labels=["release"])
        args = ("release.py", "preview", "--pr", "11", "--base-ref", "main", "--head-ref", branch, "--comment")
        out = self.assertRan(box.run(*args))
        self.assertIn("ao mesclar, este PR publica **v1.3.0** (minor)", out)
        self.assertIn("este PR e 1 mudança(s) já acumulada(s)", out)

        box.update_state(lambda state: state["pulls"]["11"].update(labels=[]))
        out = self.assertRan(box.run(*args))
        self.assertIn("este PR não publica versão", out)
        self.assertIn("Com `release`, o merge publicaria **v1.3.0** (minor)", out)
        comments = _bodies(box.state(), 11)
        self.assertEqual(len(comments), 1)
        self.assertIn("não publica versão", comments[0])

    def test_a_preview_longer_than_a_comment_accepts_drops_the_table(self):
        box = self.box
        padding = " detalhe" * 500  # cada linha da tabela leva o título: 20 mudanças passam de 65 536
        for number in range(10, 30):
            box.merge_pr(number, f"fix: case {number}{padding}", [(f"fix: case {number}", None)])
        branch = box.open_pr(40, "feat: add the export command", FEAT, labels=["release"])
        args = ("release.py", "preview", "--pr", "40", "--base-ref", "main", "--head-ref", branch, "--comment")
        out = self.assertRan(box.run(*args))
        self.assertIn("| Mudança | Nível | Sinais |", out)  # a saída do check continua completa
        self.assertNotIn("não consegui comentar", out)
        comments = _bodies(box.state(), 40)
        self.assertEqual(len(comments), 1)
        self.assertLessEqual(len(comments[0]), 65_536)
        self.assertIn("ao mesclar, este PR publica **v1.3.0** (minor)", comments[0])
        self.assertIn("não cabe num comentário", comments[0])
        self.assertNotIn("| Mudança | Nível | Sinais |", comments[0])

    def test_check_reads_the_pr_tree_with_the_base_configuration(self):
        box = self.box
        tree = box.base / "pr-head"
        for name, content in {**BASE_FILES, "package.json": '{\n  "version": "9.9.9"\n}\n'}.items():
            (tree / name).parent.mkdir(parents=True, exist_ok=True)
            (tree / name).write_bytes(content.encode("utf-8"))
        refused = box.run("release.py", "check", "--tree", str(tree))
        self.assertEqual(refused.returncode, 1)
        self.assertIn("package.json", refused.stdout)
        (tree / "package.json").write_bytes(BASE_FILES["package.json"].encode("utf-8"))
        self.assertIn(
            "coerentes com a versão 1.2.0", self.assertRan(box.run("release.py", "check", "--tree", str(tree)))
        )
        self.assertEqual(box.run("release.py", "check", "--tree", str(box.base / "missing")).returncode, 1)

    def test_check_compares_an_older_pr_with_the_version_it_started_from(self):
        box = self.box
        box.git("switch", "-q", "-c", "work/20")  # o PR sai de v1.2.0 e não toca na versão
        box.write({"notes/older.txt": "x"})
        box.git("add", "-A")
        box.git("commit", "-q", "-m", "feat: older work")
        box.git("push", "-q", "origin", "work/20")
        box.git("switch", "-q", "main")
        box.merge_pr(10, "fix: handle the empty list", FIX, labels=["release"])
        self.assertRan(box.release())  # a base passa a v1.2.1
        tree = box.base / "pr-head"
        box.git("clone", "-q", "--branch", "work/20", str(box.remote), str(tree))

        out = self.assertRan(box.run("release.py", "check", "--tree", str(tree)))
        self.assertIn("coerentes com a versão 1.2.0 (tag v1.2.0, a maior alcançável da ponta do PR)", out)

        # editar a versão à mão continua reprovado, mesmo que seja para a versão corrente da base
        (tree / "package.json").write_bytes(BASE_FILES["package.json"].replace("1.2.0", "1.2.1").encode("utf-8"))
        refused = box.run("release.py", "check", "--tree", str(tree))
        self.assertEqual(refused.returncode, 1)
        self.assertIn("package.json", refused.stdout)

        # atualizado com a base, o PR passa a ser conferido pela versão nova
        Git(tree)("checkout", "-q", "--", "package.json")
        Git(tree)("-c", "user.name=a", "-c", "user.email=a@example.com", "merge", "-q", "--no-edit", "origin/main")
        self.assertIn(
            "coerentes com a versão 1.2.1", self.assertRan(box.run("release.py", "check", "--tree", str(tree)))
        )

    def test_the_preview_before_the_base_tag_explains_and_succeeds(self):
        box = Sandbox(tag=None)
        self.addCleanup(box.close)
        branch = box.open_pr(11, "feat: add the export command", FEAT, labels=["release"])
        args = ("release.py", "preview", "--pr", "11", "--base-ref", "main", "--head-ref", branch, "--comment")
        out = self.assertRan(box.run(*args))
        self.assertIn("a prévia começa depois de `release.py baseline`", out)
        self.assertEqual(_bodies(box.state(), 11), [])

    def test_without_github_the_history_alone_still_gives_a_plan(self):
        box = self.box
        box.merge_pr(10, "feat: add the export command", FEAT)
        completed = box.run("release.py", "plan", env={"DEV_WORKFLOW_OFFLINE": "1"})
        self.assertIn("**v1.3.0** (minor)", self.assertRan(completed))
        self.assertEqual(self.assertRan(box.run("release.py", "next", "--level", "major")).strip(), "2.0.0")


if __name__ == "__main__":
    unittest.main()
