"""Pré-release pedida por label no PR: canais alpha, beta e rc, de ponta a ponta.

Mesmo arranjo de ``test_e2e_release.py``: repositório git temporário com remoto *bare*, PRs
mesclados como o GitHub faria e ``release.py`` de verdade, em subprocesso, contra um ``gh`` simulado.
"""

import json
import re
import unittest

import support  # noqa: F401  (põe scripts/ no sys.path antes de importar devflow)
from support import BASE_CONFIG, BASE_FILES, PR_BODY, Sandbox, SandboxCase

FIX = [("fix: handle the empty list", None)]
FEAT = [("feat: add the export command", None)]
BREAKING = [("feat!: drop the legacy client", None)]
API = [("fix: require the page size", {"api/client.py": "def fetch(size):\n    return size\n"})]
ANALYSIS = (
    "<!-- dev-workflow:analysis\n"
    '{"schema": 1, "base": "v1.2.0", "head": "abc1234", "changes": '
    '[{"pr": %d, "level": "patch", "summary": "o argumento novo tem valor padrão", "evidence": []}]}\n'
    "-->\nanálise"
)


def _released_marks(state, number, tag):
    comments = state["comments"].get(str(number), [])
    return [comment for comment in comments if f"dev-workflow:released {tag}" in comment["body"]]


class Timeline:
    """A linha do tempo do plano, igual nos dois modos de release; só muda como a versão sai."""

    def _open_release_pr(self):
        (pull,) = [pull for pull in self.box.state()["pulls"].values() if pull["state"] == "open"]
        return pull

    def _publish(self, tag):
        """O que o workflow faz depois de um push. No modo por PR, o PR de release é mesclado aqui."""
        box = self.box
        out = self.assertRan(box.release())
        if self.mode == "pull-request":
            pull = self._open_release_pr()
            self.assertEqual(pull["title"], f"chore(release): {tag}")
            number = pull["number"]
            box.git("fetch", "-q", "origin", "release/next")
            subject = f"Merge pull request #{number} from acme/release/next"
            box.git("merge", "-q", "--no-ff", "FETCH_HEAD", "-m", subject)
            box.git("push", "-q", "origin", "main")
            box.update_state(
                lambda state: state["pulls"][str(number)].update(state="closed", merged=True, merged_at="x")
            )
            out += self.assertRan(box.release())
        self.assertIn(tag, box.remote_tags())
        return out

    def test_alpha_beta_rc_and_the_final_release(self):
        box = self.box
        box.issue(1, "Exportar em CSV", "feature")
        box.merge_pr(31, "feat: add the export command", FEAT, labels=["release:alpha"], closes=[1])
        self._publish("v1.3.0-alpha.1")
        state = box.state()
        release = state["releases"]["v1.3.0-alpha.1"]
        self.assertEqual((release["prerelease"], release["make_latest"]), (True, "false"))
        self.assertIn('version = "1.3.0a1"', box.read("pyproject.toml"))
        self.assertIn('"version": "1.3.0-alpha.1"', box.read("package.json"))
        self.assertFalse((box.work / "CHANGELOG.md").exists())
        self.assertEqual(_released_marks(state, 31, "v1.3.0-alpha.1"), [])
        self.assertEqual(state["milestones"], [])

        # PR sem label depois de uma pré-release: nada sai, e o pedido do PR 31 já foi atendido
        box.merge_pr(32, "fix: handle the empty list", FIX)
        out = self.assertRan(box.release())
        self.assertIn("Nada a versionar: 2 mudança(s) acumulada(s) desde v1.2.0, 1 depois de v1.3.0-alpha.1", out)
        self.assertEqual(box.remote_tags(), ["v1.2.0", "v1.3.0-alpha.1"])

        box.merge_pr(33, "feat: add the import command", [("feat: add the import", None)], labels=["release:alpha"])
        self._publish("v1.3.0-alpha.2")

        box.merge_pr(34, "fix: round half up", [("fix: round half up", None)], labels=["release:beta"])
        self._publish("v1.3.0-beta.1")  # promoção: a numeração recomeça
        self.assertIn('version = "1.3.0b1"', box.read("pyproject.toml"))

        box.merge_pr(35, "feat!: drop the legacy client", BREAKING, labels=["release:beta"])
        out = self._publish("v2.0.0-beta.1")
        self.assertIn("O alvo do ciclo mudou de 1.3.0 para 2.0.0", out)

        box.merge_pr(36, "fix: keep the order", [("fix: keep the order", None)], labels=["release:rc"])
        self._publish("v2.0.0-rc.1")
        self.assertEqual(box.state()["milestones"], [])

        box.merge_pr(37, "docs: explain the export", [("docs: explain the export", None)], labels=["release"])
        self._publish("v2.0.0")
        self.assertEqual(
            box.remote_tags(),
            ["v1.2.0", "v1.3.0-alpha.1", "v1.3.0-alpha.2", "v1.3.0-beta.1", "v2.0.0", "v2.0.0-beta.1", "v2.0.0-rc.1"],
        )
        state = box.state()
        release = state["releases"]["v2.0.0"]
        self.assertEqual((release["prerelease"], release["make_latest"]), (False, "true"))
        self.assertIn('version = "2.0.0"', box.read("pyproject.toml"))
        # a final cobre tudo desde a última final: changelog, comentários e milestone saem com ela
        changelog = box.read("CHANGELOG.md")
        self.assertIn("compare/v1.2.0...v2.0.0", changelog)
        self.assertIn("Exportar em CSV", changelog)
        self.assertIn("drop the legacy client", changelog)
        for number in (31, 32, 33, 34, 35, 36, 37, 1):
            self.assertEqual(len(_released_marks(state, number, "v2.0.0")), 1, number)
        self.assertEqual([m["title"] for m in state["milestones"]], ["v2.0.0"])
        self.assertIn("Nada a versionar: 0 mudança(s)", self.assertRan(box.release()))


class DirectTimelineTest(Timeline, SandboxCase):
    mode = None


class PullRequestTimelineTest(Timeline, SandboxCase):
    mode = "pull-request"


class ChannelLabelTest(SandboxCase):
    def test_a_label_of_an_earlier_channel_is_promoted_instead_of_going_back(self):
        box = self.box
        box.merge_pr(31, "feat: add the export command", FEAT, labels=["release:beta"])
        self.assertRan(box.release())
        box.merge_pr(32, "fix: handle the empty list", FIX, labels=["release:alpha"])
        out = self.assertRan(box.release())
        self.assertIn("O label `release:alpha` pede um canal anterior a `beta`", out)
        self.assertIn("a pré-release sai em `beta`", out)
        self.assertEqual(box.remote_tags(), ["v1.2.0", "v1.3.0-beta.1", "v1.3.0-beta.2"])

    def test_the_most_mature_request_among_the_new_prs_wins(self):
        box = self.box
        box.merge_pr(31, "feat: add the export command", FEAT, labels=["release:alpha"])
        box.merge_pr(32, "fix: handle the empty list", FIX, labels=["release:rc"])
        out = self.assertRan(box.release())
        self.assertIn("Vence o pedido mais maduro: #32 (`release:rc`)", out)
        self.assertEqual(box.remote_tags(), ["v1.2.0", "v1.3.0-rc.1"])

    def test_a_manual_prerelease_answers_the_pending_requests_and_needs_something_new_to_repeat(self):
        box = self.box
        box.merge_pr(31, "feat: add the export command", FEAT, labels=["release:alpha"])
        out = self.assertRan(box.release("--prerelease", "beta"))
        self.assertIn("A execução manual pede a pré-release `beta`", out)
        self.assertEqual(box.remote_tags(), ["v1.2.0", "v1.3.0-beta.1"])
        self.assertIn("Nada a versionar", self.assertRan(box.release()))  # o pedido do PR 31 foi atendido

        repeated = box.release("--prerelease", "beta")
        self.assertEqual(repeated.returncode, 1)
        self.assertIn("Nada novo desde v1.3.0-beta.1", repeated.stdout)
        self.assertEqual(box.remote_tags(), ["v1.2.0", "v1.3.0-beta.1"])

        # promover e encerrar o ciclo não pedem mudança nova
        self.assertRan(box.release("--prerelease", "rc"))
        self.assertRan(box.release("--force"))
        self.assertEqual(box.remote_tags(), ["v1.2.0", "v1.3.0", "v1.3.0-beta.1", "v1.3.0-rc.1"])
        self.assertIn("add the export command", box.read("CHANGELOG.md"))

    def test_an_override_holds_for_the_pr_that_carries_it_and_the_cycle_keeps_the_target(self):
        box = self.box
        body = PR_BODY.format(issues="Closes #", summary="x", version="Motivo da sobreposição: API nova declarada.")
        box.merge_pr(31, "fix: handle the empty list", FIX, labels=["release:beta", "semver:major"], body=body)
        out = self.assertRan(box.release())
        self.assertIn("Nível sobreposto para **major** (label `semver:major` em #31)", out)
        self.assertEqual(box.remote_tags(), ["v1.2.0", "v2.0.0-beta.1"])

        box.merge_pr(32, "fix: round half up", [("fix: round half up", None)], labels=["release:rc"])
        out = self.assertRan(box.release())
        self.assertNotIn("Nível sobreposto", out)  # a sobreposição era do PR 31, que já saiu
        self.assertIn("O ciclo em andamento já publicou v2.0.0-beta.1", out)
        self.assertEqual(box.remote_tags(), ["v1.2.0", "v2.0.0-beta.1", "v2.0.0-rc.1"])

        box.merge_pr(33, "docs: explain the export", [("docs: explain the export", None)], labels=["release"])
        self.assertRan(box.release())
        self.assertIn("v2.0.0", box.remote_tags())

    def test_a_channel_label_works_on_a_maintenance_line(self):
        box = self.box
        box.merge_pr(10, "feat: add the export command", FEAT, labels=["release"])
        self.assertRan(box.release())
        box.git("switch", "-q", "-c", "release/1.2", "v1.2.0")
        box.git("push", "-q", "origin", "release/1.2")
        box.merge_pr(11, "fix: handle the empty list", FIX, labels=["release:rc"], base="release/1.2")
        self.assertRan(box.release())
        self.assertEqual(box.remote_tags(), ["v1.2.0", "v1.2.1-rc.1", "v1.3.0"])
        self.assertIn('version = "1.2.1rc1"', box.read("pyproject.toml"))

        box.merge_pr(12, "fix: round half up", [("fix: round half up", None)], labels=["release"], base="release/1.2")
        self.assertRan(box.release())
        self.assertEqual(box.remote_tags(), ["v1.2.0", "v1.2.1", "v1.2.1-rc.1", "v1.3.0"])
        self.assertEqual(box.state()["releases"]["v1.2.1"]["make_latest"], "false")

    def test_status_shows_the_cycle_and_the_json_plan_says_what_is_new(self):
        box = self.box
        box.merge_pr(31, "feat: add the export command", FEAT, labels=["release:alpha"])
        self.assertRan(box.release())
        box.merge_pr(32, "fix: handle the empty list", FIX)

        out = self.assertRan(box.run("release.py", "status"))
        self.assertIn("**v1.3.0** (minor) — seria publicada", out)
        self.assertIn("Ciclo de pré-releases em andamento: v1.3.0-alpha.1. Versão corrente: v1.3.0-alpha.1.", out)

        plan = json.loads(self.assertRan(box.run("release.py", "plan", "--format", "json")))
        self.assertEqual((plan["type"], plan["channel"], plan["requested"]), ("final", None, False))
        self.assertEqual((plan["previous_tag"], plan["cycle"]), ("v1.3.0-alpha.1", ["v1.3.0-alpha.1"]))
        fresh = [(change["key"], change["fresh"]) for change in plan["changes"]]
        self.assertEqual(fresh, [("#31", False), ("#32", True)])

        branch = box.open_pr(33, "fix: round half up", [("fix: round half up", None)], labels=["release:beta"])
        args = ("release.py", "plan", "--format", "json", "--pr", "33", "--base-ref", "main", "--head-ref", branch)
        plan = json.loads(self.assertRan(box.run(*args)))
        self.assertEqual((plan["type"], plan["channel"], plan["next_tag"]), ("prerelease", "beta", "v1.3.0-beta.1"))
        self.assertEqual(plan["requested_by"], ["#33"])


class RequestRecoveryTest(SandboxCase):
    config = BASE_CONFIG.replace("[versioning]", "[versioning]\nrequire_analysis = true")

    def test_a_request_that_failed_is_answered_by_the_next_push(self):
        box = self.box
        box.merge_pr(31, "fix: require the page size", API, labels=["release:beta"])
        failed = box.release()
        self.assertEqual(failed.returncode, 1)
        self.assertIn("sem análise registrada: #31", failed.stdout)
        self.assertEqual(box.remote_tags(), ["v1.2.0"])

        box.comment(31, (ANALYSIS % 31).replace("abc1234", box.head_of(31)))
        box.merge_pr(32, "fix: handle the empty list", FIX)  # o push seguinte, de um PR sem label
        out = self.assertRan(box.release())
        self.assertIn("Versão pedida por: #31.", out)
        self.assertEqual(box.remote_tags(), ["v1.2.0", "v1.2.1-beta.1"])
        self.assertIn("Nada a versionar", self.assertRan(box.release()))

    def _check(self, number, branch):
        args = ("release.py", "preview", "--pr", str(number), "--base-ref", "main", "--head-ref", branch, "--check")
        return self.box.run(*args)

    def test_the_request_check_passes_what_can_be_published_and_waits_for_the_analysis(self):
        box = self.box
        plain = box.open_pr(41, "fix: handle the empty list", FIX)
        out = self.assertRan(self._check(41, plain))
        self.assertIn("PR #41 sem label de versão", out)

        asking = box.open_pr(42, "feat: add the export command", FEAT, labels=["release:beta"])
        out = self.assertRan(self._check(42, asking))
        self.assertIn("PR #42: o pedido `release:beta` pode ser publicado (v1.3.0-beta.1).", out)
        self.assertEqual(box.state()["comments"], {})  # o check não comenta

        # O PR toca a superfície pública e pede versão antes de a análise existir. O check avisa e não
        # reprova: a análise chega num comentário, que não o dispara de novo, e ele ficaria vermelho.
        surface = box.open_pr(43, "fix: require the page size", API, labels=["release"])
        out = self.assertRan(self._check(43, surface))
        self.assertIn("aviso: Mudanças em superfícies públicas sem análise registrada: #43", out)
        self.assertIn("PR #43: o pedido `release` aguarda a análise de versão", out)
        self.assertNotIn("pode ser publicado", out)
        box.comment(43, (ANALYSIS % 43).replace("abc1234", box.head_of(43)))
        self.assertIn("pode ser publicado (v1.2.1)", self.assertRan(self._check(43, surface)))
        # sem label de versão o mesmo PR não pede nada, e o check não tem o que conferir
        box.update_state(lambda state: state["pulls"]["43"].update(labels=[]))
        self.assertIn("sem label de versão", self.assertRan(self._check(43, surface)))

    def test_the_release_still_stops_for_the_analysis_the_check_only_warned_about(self):
        box = self.box
        box.merge_pr(43, "fix: require the page size", API, labels=["release"])
        refused = box.release()
        self.assertEqual(refused.returncode, 1)
        self.assertIn("sem análise registrada: #43", refused.stdout)
        self.assertEqual(box.remote_tags(), ["v1.2.0"])

    def test_a_pr_opened_before_another_release_is_checked_against_the_tip_of_the_base(self):
        box = self.box
        opened_at = box.git("rev-parse", "main")
        branch = box.open_pr(42, "fix: round half up", [("fix: round half up", None)], labels=["release"])
        box.merge_pr(31, "fix: handle the empty list", FIX, labels=["release"])
        self.assertRan(box.release())
        self.assertEqual(box.remote_tags(), ["v1.2.0", "v1.2.1"])

        # É o que o workflow passa: a ponta atual da branch de destino, onde v1.2.1 já saiu.
        out = self.assertRan(self._check(42, branch))
        self.assertIn("PR #42: o pedido `release` pode ser publicado (v1.2.2).", out)
        # O SHA de base do evento fica no commit em que o PR foi aberto: dali, o número já está tomado,
        # e o check reprovaria um PR que não tem nada de errado.
        args = ("release.py", "preview", "--pr", "42", "--base-ref", opened_at, "--head-ref", branch, "--check")
        stale = box.run(*args)
        self.assertEqual(stale.returncode, 1)
        self.assertIn("A tag v1.2.1 já existe", stale.stdout)

    def test_an_inherited_problem_stays_inherited_when_the_pr_changes_the_target(self):
        floors = '\n[[versioning.floors]]\nglobs = ["api/**"]\nregex = \'since="(\\d+\\.\\d+\\.\\d+)"\'\n'
        files = {**BASE_FILES, "api/client.py": 'def fetch():\n    return 1  # since="3.0.0"\n'}
        box = Sandbox(BASE_CONFIG + floors, files, tag="v1.2.0")
        self.addCleanup(box.close)
        box.merge_pr(31, "fix: handle the empty list", FIX)  # o piso 3.0.0 já impede a release sem o PR 42
        status = self.assertRan(box.run("release.py", "status"))
        self.assertIn("declara o piso 3.0.0, maior que a versão 1.2.1", status)

        # O PR leva o alvo de 1.2.1 a 1.3.0, e o texto do problema muda com ele; o problema é o mesmo.
        branch = box.open_pr(42, "feat: add the export command", FEAT, labels=["release"])
        args = ("release.py", "preview", "--pr", "42", "--base-ref", "main", "--head-ref", branch, "--check")
        out = self.assertRan(box.run(*args))
        self.assertIn("aviso: `api/client.py` declara o piso 3.0.0, maior que a versão 1.3.0", out)
        self.assertIn("PR #42: o pedido `release` esbarra em 1 problema(s) que já existem sem este PR", out)

    def test_a_problem_that_exists_without_the_pr_does_not_fail_its_check(self):
        box = self.box
        box.merge_pr(31, "fix: require the page size", API)  # já na branch, sem análise: o problema é anterior
        asking = box.open_pr(42, "feat: add the export command", FEAT, labels=["release:rc"])
        out = self.assertRan(self._check(42, asking))
        self.assertIn("aviso: Mudanças em superfícies públicas sem análise registrada: #31", out)
        self.assertIn("PR #42: o pedido `release:rc` esbarra em 1 problema(s) que já existem sem este PR", out)

    def test_an_override_below_the_cycle_fails_the_check_of_the_pr_that_asks_it(self):
        box = self.box
        box.merge_pr(31, "feat!: drop the legacy client", BREAKING, labels=["release:beta"])
        self.assertRan(box.release())
        body = PR_BODY.format(issues="Closes #", summary="x", version="Motivo da sobreposição: é só uma correção.")
        branch = box.open_pr(42, "fix: handle the empty list", FIX, labels=["release", "semver:patch"], body=body)
        refused = self._check(42, branch)
        self.assertEqual(refused.returncode, 1)
        self.assertIn("A sobreposição pede 1.2.1, abaixo de 2.0.0", refused.stdout)


class ChannelPreviewTest(SandboxCase):
    def _preview(self, number, branch):
        args = ("release.py", "preview", "--pr", str(number), "--base-ref", "main", "--head-ref", branch)
        return self.assertRan(self.box.run(*args))

    def test_the_preview_says_what_the_merge_publishes_in_each_case(self):
        box = self.box
        box.merge_pr(30, "fix: handle the empty list", FIX)

        # sem label: o texto de sempre, mais a saída pelos canais
        plain = box.open_pr(41, "feat: add the export command", FEAT)
        out = self._preview(41, plain)
        self.assertIn("### Prévia de versão: este PR não publica versão", out)
        self.assertIn("Com `release`, o merge publicaria **v1.3.0** (minor).", out)
        self.assertIn("Com um label de canal (`release:alpha`, `release:beta`, `release:rc`)", out)
        self.assertNotIn("Ciclo de pré-releases", out)

        # com label de canal
        box.update_state(lambda state: state["pulls"]["41"].update(labels=[{"name": "release:alpha"}]))
        out = self._preview(41, plain)
        self.assertIn("ao mesclar, este PR publica a pré-release **v1.3.0-alpha.1** (minor)", out)
        self.assertIn("Entram nela este PR e 1 mudança(s) já acumulada(s) desde v1.2.0.", out)
        self.assertIn("É uma versão de teste do canal `alpha`", out)
        self.assertNotIn("Novidade desde", out)

        # com um ciclo aberto: o que é novidade, e o que `release` encerra
        box.merge_pr(31, "feat: add the import command", [("feat: add the import", None)], labels=["release:alpha"])
        self.assertRan(box.release())
        box.merge_pr(32, "fix: round half up", [("fix: round half up", None)])
        later = box.open_pr(42, "fix: keep the order", [("fix: keep the order", None)], labels=["release:alpha"])
        out = self._preview(42, later)
        self.assertIn("publica a pré-release **v1.3.0-alpha.2** (minor)", out)
        self.assertIn("Novidade desde v1.3.0-alpha.1: este PR e 1 mudança(s) que ainda não saíram em pré-release.", out)
        self.assertIn("Ciclo de pré-releases em andamento: v1.3.0-alpha.1.", out)

        box.update_state(lambda state: state["pulls"]["42"].update(labels=[{"name": "release"}]))
        out = self._preview(42, later)
        self.assertIn("### Prévia de versão: ao mesclar, este PR publica **v1.3.0** (minor)", out)
        self.assertIn("Ela encerra o ciclo de pré-releases em andamento (v1.3.0-alpha.1)", out)

        # sem label, mas outro PR já pediu versão e ela ainda não saiu
        box.update_state(lambda state: state["pulls"]["42"].update(labels=[]))
        box.update_state(lambda state: state["pulls"]["32"].update(labels=[{"name": "release:beta"}]))
        out = self._preview(42, later)
        self.assertIn("este PR não pede versão, mas entra em **v1.3.0-beta.1** (minor)", out)
        self.assertIn("Há pedido de versão ainda não atendido (#32 (`release:beta`))", out)


class WorkflowOutputsTest(SandboxCase):
    config = BASE_CONFIG + '\n[release]\ndispatch_workflow = "publish.yml"\n'

    def _outputs(self, box):
        """O que uma execução grava em ``GITHUB_OUTPUT``, para os jobs seguintes do workflow."""
        path = box.base / "github-output.txt"
        path.write_text("", encoding="utf-8")
        self.assertRan(box.release(env={"GITHUB_OUTPUT": str(path)}))
        return dict(line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines())

    def test_the_outputs_say_whether_the_version_is_a_prerelease_and_of_which_channel(self):
        box = self.box
        box.merge_pr(31, "feat: add the export command", FEAT, labels=["release:beta"])
        self.assertEqual(
            self._outputs(box),
            {
                "released": "true",
                "version": "1.3.0-beta.1",
                "tag": "v1.3.0-beta.1",
                "prerelease": "true",
                "channel": "beta",
            },
        )
        box.merge_pr(32, "fix: handle the empty list", FIX, labels=["release"])
        self.assertEqual(
            self._outputs(box),
            {"released": "true", "version": "1.3.0", "tag": "v1.3.0", "prerelease": "false", "channel": ""},
        )
        self.assertEqual(self._outputs(box), {"released": "false"})
        # o workflow de publicação continua recebendo só `version` e `tag`: ele deriva o resto da versão
        self.assertEqual(
            [dispatch["inputs"] for dispatch in box.state()["dispatches"]],
            [{"version": "1.3.0-beta.1", "tag": "v1.3.0-beta.1"}, {"version": "1.3.0", "tag": "v1.3.0"}],
        )

    def test_a_prepared_release_pr_announces_the_version_it_will_publish(self):
        box = Sandbox(BASE_CONFIG, tag="v1.2.0", mode="pull-request")
        self.addCleanup(box.close)
        box.merge_pr(31, "feat: add the export command", FEAT, labels=["release:rc"])
        announced = {"version": "1.3.0-rc.1", "tag": "v1.3.0-rc.1", "prerelease": "true", "channel": "rc"}
        self.assertEqual(self._outputs(box), {"released": "false", **announced})

        # Mesclado o PR de release, a execução publica a tag e ainda confere se sobrou algo a versionar:
        # essa segunda rodada não apaga o `released=true`, que é o que dispara os jobs de publicação.
        (pull,) = [pull for pull in box.state()["pulls"].values() if pull["state"] == "open"]
        number = str(pull["number"])
        box.merge_pr(32, "fix: handle the empty list", FIX, labels=["release:rc"])  # entrou antes do merge
        box.git("fetch", "-q", "origin", "release/next")
        subject = f"Merge pull request #{number} from acme/release/next"
        box.git("merge", "-q", "--no-ff", "-X", "theirs", "FETCH_HEAD", "-m", subject)
        box.git("push", "-q", "origin", "main")
        box.update_state(lambda state: state["pulls"][number].update(state="closed", merged=True, merged_at="x"))
        self.assertEqual(self._outputs(box), {"released": "true", **announced})
        self.assertEqual(box.remote_tags(), ["v1.2.0", "v1.3.0-rc.1"])
        # ...nem o PR de release seguinte, aberto na mesma execução para o pedido do PR 32.
        (pull,) = [pull for pull in box.state()["pulls"].values() if pull["state"] == "open"]
        self.assertEqual(pull["title"], "chore(release): v1.3.0-rc.2")


WHY = "### Por que este número"


class PrereleaseNotesTest(SandboxCase):
    def test_the_notes_open_with_what_is_new_and_a_resumed_release_says_the_same(self):
        box = self.box
        box.merge_pr(31, "feat: add the export command", FEAT, labels=["release:beta"])
        self.assertRan(box.release())
        first = box.state()["releases"]["v1.3.0-beta.1"]["body"]
        self.assertTrue(first.startswith("### Funcionalidades\n\n- add the export command"), first)
        self.assertNotIn("Novidades desde", first)

        box.merge_pr(32, "fix: handle the empty list", FIX)
        box.merge_pr(33, "fix: round half up", [("fix: round half up", None)], labels=["release:beta"])
        # a criação da GitHub Release falha depois da tag: a execução seguinte conclui pelo caminho de retomada
        box.update_state(lambda state: state.__setitem__("fail_release_once", True))
        self.assertEqual(box.release().returncode, 1)
        self.assertNotIn("v1.3.0-beta.2", box.state()["releases"])
        annotation = box.git("tag", "--list", "--format=%(contents)", "v1.3.0-beta.2")
        written = annotation.split("\n\n", 1)[1].split(WHY)[0]
        self.assertTrue(written.startswith("## Novidades desde v1.3.0-beta.1\n\n### Correções\n\n"), written)
        news, accumulated = written.split("## Acumulado desde v1.2.0\n\n")
        self.assertIn("handle the empty list", news)
        self.assertIn("round half up", news)
        self.assertNotIn("add the export command", news)
        for title in ("add the export command", "handle the empty list", "round half up"):
            self.assertIn(title, accumulated)
        self.assertFalse((box.work / "CHANGELOG.md").exists())

        out = self.assertRan(box.release())
        self.assertIn("A tag v1.3.0-beta.2 não tem GitHub Release; concluindo a publicação.", out)
        resumed = box.state()["releases"]["v1.3.0-beta.2"]["body"]
        self.assertEqual(resumed.split(WHY)[0], written)
        self.assertNotIn("CHANGELOG.md", resumed)

        # promoção sem nada novo: as notas dizem, e o acumulado continua inteiro
        self.assertRan(box.release("--prerelease", "rc"))
        promoted = box.state()["releases"]["v1.3.0-rc.1"]["body"]
        self.assertTrue(promoted.startswith("## Novidades desde v1.3.0-beta.2\n\nNada novo desde v1.3.0-beta.2"))
        self.assertIn("round half up", promoted.split("## Acumulado desde v1.2.0")[1])

        # a final não separa: ela é a lista inteira desde a última final
        box.merge_pr(34, "docs: explain the export", [("docs: explain the export", None)], labels=["release"])
        self.assertRan(box.release())
        final = box.state()["releases"]["v1.3.0"]["body"]
        self.assertNotIn("Novidades desde", final)
        self.assertIn("add the export command", final)


class ChangelogWithPrereleasesTest(SandboxCase):
    config = BASE_CONFIG + "\n[changelog]\nprereleases = true\n"
    old = "# Changelog\n\nTexto do projeto.\n\n## [1.2.0] — 2030-01-01\n\n- antiga\n"
    files = {**BASE_FILES, "CHANGELOG.md": old}

    def _sections(self):
        return re.findall(r"^## \[([^\]]+)\]\(([^)]*)\)", self.box.read("CHANGELOG.md"), re.MULTILINE)

    def test_the_file_keeps_one_section_per_version_without_leftovers_of_the_cycle(self):
        box = self.box
        box.merge_pr(31, "feat: add the export command", FEAT, labels=["release:beta"])
        self.assertRan(box.release())
        self.assertEqual(
            self._sections(), [("1.3.0-beta.1", "https://github.com/acme/demo/compare/v1.2.0...v1.3.0-beta.1")]
        )

        box.merge_pr(32, "fix: handle the empty list", FIX, labels=["release:beta"])
        self.assertRan(box.release())
        # uma seção só, com a lista inteira; o link compara com a pré-release anterior
        self.assertEqual(
            self._sections(),
            [("1.3.0-beta.2", "https://github.com/acme/demo/compare/v1.3.0-beta.1...v1.3.0-beta.2")],
        )
        changelog = box.read("CHANGELOG.md")
        self.assertEqual(changelog.count("add the export command"), 1)
        self.assertIn("handle the empty list", changelog)

        box.merge_pr(33, "feat!: drop the legacy client", BREAKING, labels=["release:rc"])
        self.assertRan(box.release())
        self.assertEqual([name for name, _ in self._sections()], ["2.0.0-rc.1"])  # o alvo mudou: 1.3.0-beta.* sai

        box.merge_pr(34, "docs: explain the export", [("docs: explain the export", None)], labels=["release"])
        self.assertRan(box.release())
        self.assertEqual(self._sections(), [("2.0.0", "https://github.com/acme/demo/compare/v1.2.0...v2.0.0")])
        changelog = box.read("CHANGELOG.md")
        self.assertEqual(changelog.count("add the export command"), 1)
        self.assertTrue(changelog.startswith("# Changelog\n\nTexto do projeto.\n\n## [2.0.0]("))
        self.assertTrue(changelog.endswith("\n\n## [1.2.0] — 2030-01-01\n\n- antiga\n"))


if __name__ == "__main__":
    unittest.main()
