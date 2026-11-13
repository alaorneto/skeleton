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


class AgentAnalysisTest(SandboxCase):
    ANALYSIS = (
        "<!-- dev-workflow:analysis\n"
        '{"schema": 1, "base": "v1.2.0", "head": "abc1234", "changes": '
        '[{"pr": 10, "level": "major", "summary": "fetch() passou a exigir um argumento", '
        '"evidence": ["api/client.py: assinatura de fetch"]}]}\n'
        "-->\nanálise"
    )
    COMMITS = [("fix: require the page size", {"api/client.py": "def fetch(size):\n    return size\n"})]

    def test_a_recorded_analysis_raises_the_level(self):
        box = self.box
        box.merge_pr(10, "fix: require the page size", self.COMMITS, labels=["release"])
        box.comment(10, self.ANALYSIS)
        out = self.assertRan(box.release())
        self.assertIn("fetch() passou a exigir um argumento → major", out)
        self.assertIn("Análise por agente: cobre todas as mudanças.", out)
        self.assertEqual(box.tags(), ["v1.2.0", "v2.0.0"])

    def test_an_analysis_from_someone_without_write_access_is_ignored(self):
        box = self.box
        box.merge_pr(10, "fix: require the page size", self.COMMITS, labels=["release"])
        box.comment(10, self.ANALYSIS, login="mallory", association="NONE")
        out = self.assertRan(box.release())
        self.assertIn("superfície pública tocada sem análise: `api/client.py`", out)
        self.assertIn("Análise por agente: ausente", out)
        self.assertEqual(box.tags(), ["v1.2.0", "v1.2.1"])

    def test_the_analysis_never_lowers_what_the_signals_say(self):
        box = self.box
        box.merge_pr(10, "feat: add the export command", FEAT, labels=["release"])
        box.comment(10, self.ANALYSIS.replace('"major"', '"patch"'))
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
        box.comment(10, self.ANALYSIS.replace('"major"', '"patch"'))
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
        self.assertEqual(box.git("rev-list", "-n", "1", "v1.2.1"), box.git("rev-parse", "HEAD"))
        self.assertIn('version = "1.2.1"', box.read("pyproject.toml"))
        state = box.state()
        self.assertIn("Corrigir a lista vazia", state["releases"]["v1.2.1"]["body"])
        self.assertEqual(len(_bodies(state, 10)), 1)
        self.assertIn("Nada a versionar", self.assertRan(box.release()))

    def test_a_squashed_release_pr_publishes_too(self):
        box = self._prepare()
        box.git("fetch", "-q", "origin", "release/next")
        box.git("merge", "-q", "--squash", "FETCH_HEAD")
        box.git("commit", "-q", "-m", "chore(release): v1.2.1 (#1001)")
        box.git("push", "-q", "origin", "main")
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
