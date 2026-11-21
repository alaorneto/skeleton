"""Modelos em inglês e nomes de seção equivalentes entre idiomas."""

import re
import tomllib
import unittest

import support  # noqa: F401  (põe scripts/ no sys.path antes de importar devflow)
from devflow import i18n
from devflow.bootstrap import PR_CHECK, planned_files, render, template_path, template_values
from devflow.config import Config
from devflow.gh import Git
from devflow.issues import check_pull_request
from devflow.text import find_section, section, sections
from i18n_support import PR_BODY_EN, english_config
from support import PR_BODY, SCRIPTS, Sandbox, SandboxCase

ASSETS = SCRIPTS.parent / "assets"
FORMS = {"1-feature.yml": "feature", "2-enhancement.yml": "enhancement", "3-bug.yml": "bug",
         "4-documentation.yml": "documentation", "5-maintenance.yml": None}
FIELD = re.compile(r"^  - type: (?P<type>\w+)\n    id: (?P<id>\w+)\n    attributes:\n      label: \"(?P<label>[^\"]+)\"", re.M)


def _form(language, name):
    return template_path(f"ISSUE_TEMPLATE/{name}", language).read_text(encoding="utf-8")


def _fields(language, name):
    """``[(tipo, id, rótulo, obrigatório)]`` dos campos de um formulário, na ordem."""
    text = _form(language, name)
    fields = []
    for match in FIELD.finditer(text):
        rest = text[match.end() :].split("\n  - type:", 1)[0]
        required = re.search(r"^      required: (true|false)$", rest, re.M)
        fields.append((match["type"], match["id"], match["label"], required[1] if required else None))
    return fields


class EnglishTemplatesTest(unittest.TestCase):
    def test_english_has_its_own_copy_of_every_issue_and_pr_template(self):
        portuguese = sorted(path.name for path in (ASSETS / "ISSUE_TEMPLATE").glob("*.yml"))
        english = sorted(path.name for path in (ASSETS / "locales/en/ISSUE_TEMPLATE").glob("*.yml"))
        self.assertEqual(english, portuguese)
        for name in ("pull_request_template.md", "dev-workflow.toml", *(f"ISSUE_TEMPLATE/{n}" for n in english)):
            self.assertEqual(template_path(name, "en"), ASSETS / "locales/en" / name, name)
            self.assertEqual(template_path(name, "pt-BR"), ASSETS / name, name)

    def test_form_labels_are_the_required_sections_of_their_language(self):
        for language in i18n.LANGUAGES:
            cfg = Config({"language": language})
            for name, kind in FORMS.items():
                with self.subTest(language=language, form=name):
                    labels = [label for _type, _id, label, _required in _fields(language, name)]
                    required = cfg.issue_sections(kind)
                    self.assertEqual([label for label in labels if label in required], required)

    def test_both_languages_have_the_same_fields(self):
        for name in FORMS:
            with self.subTest(name):
                portuguese = [(kind, ident, required) for kind, ident, _label, required in _fields("pt-BR", name)]
                english = [(kind, ident, required) for kind, ident, _label, required in _fields("en", name)]
                self.assertEqual(english, portuguese)
                for line in ("labels:", "  - type: markdown"):
                    pt_lines = [item for item in _form("pt-BR", name).splitlines() if item.startswith(line)]
                    en_lines = [item for item in _form("en", name).splitlines() if item.startswith(line)]
                    self.assertEqual(en_lines, pt_lines)

    def test_the_pr_template_has_exactly_the_required_sections(self):
        for language in i18n.LANGUAGES:
            with self.subTest(language):
                text = template_path("pull_request_template.md", language).read_text(encoding="utf-8")
                cfg = Config({"language": language})
                self.assertEqual(list(sections(text)), [name.casefold() for name in cfg.pr["required_sections"]])
                self.assertIn(f"{cfg.pr['override_reason_marker']}:", text)

    def test_the_english_configuration_is_valid_and_matches_the_portuguese_one(self):
        values = {"SKILL_DIR": ".", "DEFAULT_BRANCH": "main", "INITIAL_VERSION": "0.1.0", "VERSION_FILES": ""}
        parsed = {}
        for language in i18n.LANGUAGES:
            template = template_path("dev-workflow.toml", language).read_text(encoding="utf-8")
            text = render(template.replace("# __VERSION_FILES__", "__VERSION_FILES__"), values)
            self.assertNotIn("__", text)
            parsed[language] = tomllib.loads(text)
        english, portuguese = parsed["en"], parsed["pt-BR"]
        self.assertEqual(english.pop("language"), "en")
        self.assertNotIn("language", portuguese)
        cfg = Config(english | {"language": "en"})
        self.assertEqual(english["pr"]["required_sections"], Config({"language": "en"}).pr["required_sections"])
        for kind, spec in english["types"].items():
            self.assertEqual(spec["description"], Config({"language": "en"}).types[kind]["description"], kind)
        self.assertEqual(cfg.language, "en")

        def shape(value):
            if isinstance(value, dict):
                return {key: shape(item) for key, item in value.items() if key not in ("description", "required_sections")}
            return type(value).__name__

        self.assertEqual(shape(english), shape(portuguese))
        self.assertEqual({k: v for k, v in english.items() if k not in ("types", "pr")},
                         {k: v for k, v in portuguese.items() if k not in ("types", "pr")})

    def test_the_rendered_english_templates_have_no_leftover_markers(self):
        root = SCRIPTS.parent
        values = template_values(root, ".claude/skills/dev-workflow", "main", Config({"language": "en"}), Git(root))
        files = planned_files(values, selftest=True)
        self.assertIn("## Summary", files[".github/pull_request_template.md"])
        self.assertIn("Issue flow of the project", files[".github/ISSUE_TEMPLATE/config.yml"])
        self.assertIn("/blob/main/.claude/skills/dev-workflow/references/flow.md", files[".github/ISSUE_TEMPLATE/config.yml"])
        self.assertTrue(files[PR_CHECK].startswith("name: PR check\n"))
        for name, text in files.items():
            self.assertNotIn("__", text.replace("__pycache__", ""), name)


class SectionEquivalenceTest(unittest.TestCase):
    def test_a_section_name_matches_its_equivalent_in_the_other_language(self):
        self.assertEqual(section("## Version\n- none\n", "Versão"), "- none")
        self.assertEqual(section("## Versão\n- nenhum\n", "Version"), "- nenhum")
        self.assertEqual(section("### Acceptance criteria\n- [x] ok\n", "Critérios de aceite"), "- [x] ok")
        found = sections("## Versão\n- pt\n\n## Version\n- en\n")
        self.assertEqual(find_section(found, "Version"), "- en")  # o nome exato vem primeiro
        self.assertEqual(find_section(found, "Versão"), "- pt")

    def test_a_name_that_is_not_an_equivalent_is_still_missing(self):
        self.assertIsNone(section("## Release\n- x\n", "Versão"))
        self.assertIsNone(section("## Versions\n- x\n", "Version"))
        self.assertIsNone(section("## Resumo\n- x\n", "Tests run"))
        self.assertIsNone(section("## Minha seção\n- x\n", "Minha outra seção"))

    def test_every_default_section_has_its_equivalent(self):
        for group in i18n.section_groups():
            self.assertLessEqual(len(group), len(i18n.LANGUAGES), sorted(group))
        portuguese, english = Config(), Config({"language": "en"})
        pairs = [
            *zip(portuguese.pr["required_sections"], english.pr["required_sections"], strict=True),
            *zip(portuguese.issue_sections("bug"), english.issue_sections("bug"), strict=True),
            *zip(portuguese.issue_sections("feature"), english.issue_sections("feature"), strict=True),
            (portuguese.issues["items_section"], english.issues["items_section"]),
        ]
        for pt_name, en_name in pairs:
            self.assertEqual(section(f"## {en_name}\nx\n", pt_name), "x", pt_name)
            self.assertEqual(section(f"## {pt_name}\nx\n", en_name), "x", en_name)


def _issue_body(cfg, kind="feature"):
    names = cfg.issue_sections(kind)
    return "\n\n".join(
        f"## {name}\n" + ("- [x] done" if name == cfg.issues["acceptance_section"] else "content") for name in names
    )


def _pr(body):
    return {
        "number": 9,
        "title": "feat: add the export command",
        "body": body,
        "labels": [{"name": "release"}],
        "user": {"login": "alice"},
        "head": {"ref": "feat/1-export", "repo": {"full_name": "acme/demo"}},
        "base": {"ref": "main", "repo": {"full_name": "acme/demo"}},
    }


class CheckAcrossLanguagesTest(unittest.TestCase):
    def test_an_english_pr_passes_in_a_portuguese_project_and_the_other_way_round(self):
        portuguese, english = Config(), Config({"language": "en"})
        cases = (
            (portuguese, PR_BODY_EN.format(issues="Closes #1", summary="x", version="New feature"), english),
            (english, PR_BODY.format(issues="Closes #1", summary="x", version="Funcionalidade nova"), portuguese),
        )
        for cfg, body, other in cases:
            with self.subTest(cfg.language):
                fetch = {1: {"labels": [{"name": "feature"}], "body": _issue_body(other)}}.get
                findings = check_pull_request(_pr(body), "acme/demo", fetch, cfg)
                self.assertEqual((findings.errors, findings.warnings), ([], []))

    def test_a_section_that_is_no_equivalent_is_still_reported(self):
        body = PR_BODY_EN.format(issues="Closes #1", summary="x", version="New feature").replace("## Version", "## Release")
        fetch = {1: {"labels": [{"name": "feature"}], "body": _issue_body(Config())}}.get
        errors = check_pull_request(_pr(body), "acme/demo", fetch, Config()).errors
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("`## Versão`", errors[0])


class CheckPrCliTest(SandboxCase):
    config = english_config()

    def test_check_pr_in_an_english_project_accepts_a_portuguese_body(self):
        box = self.box
        box.issue(1, "Export as CSV", "feature", body=_issue_body(Config()))
        box.open_pr(12, "feat: add the export command", [("feat: add the export command", None)], closes=[1])
        self.assertIn("## Versão", box.state()["pulls"]["12"]["body"])
        completed = box.run("github_issues.py", "check-pr", "--pr", "12")
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)


class InitAndDoctorTest(unittest.TestCase):
    def test_init_in_english_then_doctor_has_nothing_about_the_templates(self):
        box = Sandbox(config="", files={"README.md": "x\n"})
        self.addCleanup(box.close)
        (box.work / ".github/dev-workflow.toml").unlink()
        box.git("commit", "-q", "-am", "chore: drop the config")
        done = box.run("bootstrap.py", "init", "--no-labels", "--language", "en")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("## Summary", box.read(".github/pull_request_template.md"))
        self.assertIn('label: "Acceptance criteria"', box.read(".github/ISSUE_TEMPLATE/1-feature.yml"))
        config = box.read(".github/dev-workflow.toml")
        self.assertIn("# Configuration of the development workflow", config)
        self.assertEqual(len(re.findall(r'(?m)^language = "en"$', config)), 1)
        box.git("add", "-A")
        box.git("commit", "-q", "-m", "chore: adopt the workflow\n\nRefs #1")
        doctor = box.run(str(box.work / ".claude/skills/dev-workflow/scripts/bootstrap.py"), "doctor")
        self.assertEqual(doctor.returncode, 0, doctor.stdout + doctor.stderr)
        pending = [line for line in doctor.stdout.splitlines() if line.startswith("  PENDING")]
        # Só o que o doctor não resolve num sandbox: a tag de base e o GitHub simulado.
        self.assertTrue(all("version tag" in line or "labels" in line or "GitHub" in line for line in pending), pending)
        self.assertIn("0 error(s)", doctor.stdout)


if __name__ == "__main__":
    unittest.main()


class CrossLanguageMarkersTest(unittest.TestCase):
    """O epic, a dependência e o motivo da sobreposição também valem no outro idioma."""

    def test_an_epic_written_in_english_is_read_in_a_portuguese_project(self):
        from devflow.epics import parse_epic_items

        plan = parse_epic_items("## Items\n- [ ] #3 x — depends on #2\n- [ ] #2 y — independent\n", "o/r")
        self.assertEqual([(item.number, item.blocked_by) for item in plan.items], [(3, (2,)), (2, ())])

    def test_the_override_reason_is_read_in_either_language(self):
        from devflow.text import marked_value

        self.assertEqual(marked_value("- Override reason: only the default", "Motivo da sobreposição"), "only the default")
        self.assertEqual(marked_value("- Motivo da sobreposição: só o padrão", "Override reason"), "só o padrão")
        self.assertEqual(marked_value("- Outro motivo: x", "Override reason"), "")
