"""Idioma de config.py, semver.py, gh.py e plan.py (#38): catraca, mensagens em inglês e o plano em inglês.

Com ``language = "en"`` os erros de configuração, os detalhes dos sinais, as notas, os problemas e
a justificativa do número saem em inglês; o JSON do plano tem as mesmas chaves e os mesmos códigos
nos dois idiomas, porque é contrato de quem o lê.
"""

import json
import unittest

import i18n_support
import support
from devflow import gh, i18n
from devflow.config import Config, ConfigError
from devflow.semver import Version, level_index, next_prerelease
from support import Sandbox, SandboxCase

MODULES = ("config.py", "semver.py", "gh.py", "plan.py")
# Símbolos do formato dos textos, não palavras: a seta da justificativa, a reticência da lista de
# caminhos e o travessão das linhas do changelog e das células vazias.
SYMBOLS = "→…—"

ENGLISH_CONFIG = i18n_support.english_config(
    support.BASE_CONFIG.replace("[versioning]", "[versioning]\nrequire_analysis = true", 1)
)
PORTUGUESE_CONFIG = support.BASE_CONFIG.replace("[versioning]", "[versioning]\nrequire_analysis = true", 1)


def _error(data):
    try:
        Config(data)
    except ConfigError as exc:
        return str(exc)
    raise AssertionError(f"Config({data!r}) was accepted")


def _shape(value):
    """As chaves de cada objeto do JSON, sem os valores: o contrato que não muda com o idioma."""
    if isinstance(value, dict):
        return {key: _shape(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_shape(item) for item in value]
    return type(value).__name__


class RatchetTest(unittest.TestCase):
    def test_no_accented_literal_is_left_outside_the_catalog(self):
        for module in MODULES:
            with self.subTest(module):
                self.assertEqual(i18n_support.accented_literals(module, allowed=SYMBOLS), [])


class ConfigMessagesTest(unittest.TestCase):
    def test_shape_and_value_errors_follow_the_language_of_the_project(self):
        cases = [
            ({"versioning": {"enabled": "yes"}}, "`versioning.enabled` must be of type boolean, not string."),
            ({"types": []}, "`types` must be a table, not list."),
            ({"versoning": {}}, "`versoning` is not a known key. Did you mean `versioning`?"),
            ({"zzz": 1}, "`zzz` is not a known key. Accepted: `changelog`,"),
            ({"schema": 2}, "`schema` must be 1."),
            ({"release": {"mode": "x"}}, "`release.mode` must be one of direct, pull-request."),
            ({"release": {"commit_message": "release"}}, "`release.commit_message` must contain `{tag}`."),
            (
                {"versioning": {"files": [{"path": "VERSION", "search": "1.0"}]}},
                "needs a `{current…}` marker (for example `version = \"{current}\"`)",
            ),
        ]
        for data, expected in cases:
            with self.subTest(expected):
                self.assertIn(expected, _error({"language": "en", **data}))

    def test_portuguese_keeps_the_previous_text(self):
        cases = [
            ({"versioning": {"enabled": "yes"}}, "`versioning.enabled` deve ser booleano, não texto."),
            ({"types": []}, "`types` deve ser uma tabela, não lista."),
            ({"versoning": {}}, "`versoning` não é uma chave conhecida. Você quis dizer `versioning`?"),
            ({"release": {"commit_message": "release"}}, "`release.commit_message` precisa conter `{tag}`."),
            (
                {"versioning": {"files": [{"path": "VERSION", "search": "1.0"}]}},
                "`versioning.files[0].search` precisa de um marcador `{current…}` (por exemplo "
                '`version = "{current}"`): sem ele a troca não muda nada.',
            ),
        ]
        for data, expected in cases:
            with self.subTest(expected):
                self.assertEqual(_error(data), f"dev-workflow.toml: {expected}")


class ModuleMessagesTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(i18n.activate, i18n.active())
        i18n.activate("en")

    def test_semver_errors_come_out_in_the_active_language(self):
        with self.assertRaises(ValueError) as raised:
            Version.parse("1.2")
        self.assertEqual(str(raised.exception), "'1.2' is not a SemVer version (major.minor.patch[-pre][+build]).")
        with self.assertRaises(ValueError) as raised:
            level_index("huge")
        self.assertEqual(str(raised.exception), "Unknown level: 'huge'. Use one of none, patch, minor, major.")
        with self.assertRaises(ValueError) as raised:
            next_prerelease(Version(1, 0, 0), "nightly", [], [])
        self.assertEqual(str(raised.exception), "Pre-release channel 'nightly' is not in the project's list (empty).")

    def test_gh_errors_come_out_in_the_active_language(self):
        self.assertIn("was not found on the PATH", gh._missing_tool(["no-such-tool"]))
        self.assertIn("the GitHub CLI is required", gh._missing_tool(["gh"]))


class EnglishPlanTest(SandboxCase):
    """``release.py plan`` num projeto em inglês, contra o mesmo histórico em português."""

    config = ENGLISH_CONFIG

    def _history(self, box):
        box.issue(1, "Handle the empty list", "bug")
        box.issue(2, "Rework the client")  # sem label de tipo: vira nota
        box.merge_pr(10, "fix: handle the empty list", [("fix: handle the empty list", None)], closes=[1])
        body = i18n_support.PR_BODY_EN.format(issues="Refs #2", summary="Rework the client", version="Breaking")
        api = [("feat!: rework the client", {"api/client.py": "def fetch(page):\n    return page\n"})]
        box.merge_pr(11, "feat!: rework the client", api, labels=["release"], body=body)

    def test_the_plan_speaks_english_and_the_json_keeps_its_keys_and_codes(self):
        box = self.box
        self._history(box)

        out = self.assertRan(box.run("release.py", "plan"))
        for text in (
            "**v1.2.0 → v2.0.0** — level **major**.",
            "#2 (cited by #11) does not have exactly one type label (none)",
            "#1 has the label `bug` → patch",
            "`fix` in PR title → patch",
            "`!` in PR title: rework the client → major",
            "| Change | Level | Signals |",
            "public surface touched without analysis: `api/client.py`",
            "Agent analysis: missing; the level comes only from deterministic signals.",
            "Changes to public surfaces without a recorded analysis: #11.",
        ):
            with self.subTest(text):
                self.assertIn(text, out)
        for text in ("tem o label", "título do PR", "Análise por agente", "nível **", "superfície pública"):
            with self.subTest(text):
                self.assertNotIn(text, out)

        english = json.loads(self.assertRan(box.run("release.py", "plan", "--format", "json")))
        box.write({".github/dev-workflow.toml": PORTUGUESE_CONFIG})
        portuguese = json.loads(self.assertRan(box.run("release.py", "plan", "--format", "json")))

        self.assertEqual(_shape(english), _shape(portuguese))
        for key in ("type", "channel", "natural_level", "level", "next_tag", "requested_by"):
            self.assertEqual(english[key], portuguese[key], key)
        self.assertEqual((english["level"], english["next_tag"]), ("major", "v2.0.0"))

        def codes(plan):
            return [
                [(signal["level"], signal["source"], signal["breaking"]) for signal in change["signals"]]
                for change in plan["changes"]
            ]

        self.assertEqual(codes(english), codes(portuguese))
        self.assertEqual(
            [problem.split(":")[0] for problem in english["problems"]],
            ["Changes to public surfaces without a recorded analysis"],
        )
        details = [signal["detail"] for change in english["changes"] for signal in change["signals"]]
        self.assertIn("#1 has the label `bug`", details)
        self.assertIn("#1 tem o label `bug`", [s["detail"] for c in portuguese["changes"] for s in c["signals"]])
        note = i18n.translate("en", "plan.note.issue_type", number=2, change="#11", found="none")
        self.assertIn(note, english["notes"])
        self.assertEqual(len(english["notes"]), len(portuguese["notes"]))

    def test_a_configuration_error_names_the_key_in_english(self):
        box = Sandbox(i18n_support.english_config() + '\n[release]\nmode = "sometimes"\n', tag="v1.2.0")
        self.addCleanup(box.close)
        completed = box.run("release.py", "plan")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("`release.mode` must be one of direct, pull-request.", completed.stdout + completed.stderr)


if __name__ == "__main__":
    unittest.main()
