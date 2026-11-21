"""Instalação em dois idiomas: ``init --language``, ``update`` que respeita o idioma e ``doctor`` que o confere."""

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401  (põe scripts/ no sys.path antes de importar devflow)
from devflow import bootstrap, i18n
from devflow.bootstrap import (
    MANIFEST,
    PR_CHECK,
    check_name_in,
    language_of_check,
    planned_files,
    required_check,
    template_path,
    template_values,
    with_language,
    workflow_texts,
)
from devflow.config import Config
from devflow.gh import Git
from i18n_support import accented_literals
from support import SCRIPTS, Sandbox

ASSETS = SCRIPTS.parent / "assets"
VENDORED = ".claude/skills/dev-workflow"


def _empty_project():
    """Um repositório sem configuração do fluxo, como o de quem vai adotá-lo."""
    box = Sandbox(config="", files={"README.md": "x\n"})
    (box.work / ".github" / "dev-workflow.toml").unlink()
    box.git("commit", "-q", "-am", "chore: drop the config")
    return box


def _adopt(box):
    box.git("add", "-A")
    box.git("commit", "-q", "-m", "chore: adopt the workflow\n\nRefs #1")


class RatchetTest(unittest.TestCase):
    def test_bootstrap_has_no_text_outside_the_catalog(self):
        self.assertEqual(accented_literals("bootstrap.py"), [])


class WorkflowTextsTest(unittest.TestCase):
    def test_the_required_check_name_is_fixed_per_language(self):
        # Mudar um destes textos troca o check obrigatório de todo projeto no próximo update.
        self.assertEqual(required_check("pt-BR"), "Verificação do PR")
        self.assertEqual(required_check("en"), "PR check")
        self.assertEqual(language_of_check("PR check"), "en")
        self.assertEqual(language_of_check("Verificação do PR"), "pt-BR")
        self.assertIsNone(language_of_check("Meu check"))

    def _rendered(self, language, skill_dir=VENDORED):
        root = SCRIPTS.parent
        values = template_values(root, skill_dir, "main", Config(), Git(root), language)
        return {name: text for name, text in planned_files(values, selftest=True).items() if "/workflows/" in name}

    def test_the_templates_hold_the_portuguese_texts_of_the_catalog(self):
        # O modelo é a fonte única: cada texto de exibição em pt-BR está nele, numa posição de exibição.
        templates = "".join(path.read_text(encoding="utf-8") for path in (ASSETS / "workflows").glob("*.yml"))
        for key, text in workflow_texts("pt-BR", "__SKILL_DIR__").items():
            with self.subTest(key):
                if key == "workflow.analysis.prompt":
                    self.assertIn(" " * 12 + text.replace("\n", "\n" + " " * 12) + "\n", templates)
                else:
                    shown = re.compile(rf'(?m)(?:^[ \t]*(?:- )?name: |description: "|echo "){re.escape(text)}(?:$| \(|")')
                    self.assertRegex(templates, shown)

    def test_another_language_changes_only_the_display_texts(self):
        # Lógica e SHAs das actions vêm de um modelo só: linha a linha, só mudam nomes, descrições e mensagens.
        portuguese, english = self._rendered("pt-BR"), self._rendered("en")
        display = re.compile(r'^\s*(?:- )?name: |description: "|echo "')
        texts = workflow_texts("pt-BR", VENDORED)
        prompt = texts.pop("workflow.analysis.prompt").splitlines()
        self.assertEqual(sorted(portuguese), sorted(english))
        for name in portuguese:
            pt_lines = [line for line in portuguese[name].splitlines() if line.strip() not in prompt]
            en_lines = english[name].splitlines()
            if name.endswith("semver-analysis.yml"):
                en_prompt = workflow_texts("en", VENDORED)["workflow.analysis.prompt"].splitlines()
                en_lines = [line for line in en_lines if line.strip() not in en_prompt]
            self.assertEqual(len(pt_lines), len(en_lines), name)
            for pt_line, en_line in zip(pt_lines, en_lines, strict=True):
                if pt_line != en_line:
                    self.assertRegex(pt_line, display, name)
                    self.assertTrue(any(text in pt_line for text in texts.values()), pt_line)
            for text in texts.values():
                self.assertNotRegex(english[name], rf'(?m)(?:name: |description: "|echo "){re.escape(text)}(?:$| \(|")')

    def test_display_names_are_safe_in_yaml_and_in_the_shell(self):
        # Os de `description:` e de `echo` ficam entre aspas duplas; os outros entram sem aspas no YAML.
        quoted = {key for key in workflow_texts("pt-BR", ".") if ".input." in key or "no_" in key or "echo" in key}
        for language in i18n.LANGUAGES:
            for key, value in workflow_texts(language, ".").items():
                if key == "workflow.analysis.prompt":
                    continue  # bloco literal do YAML
                with self.subTest(language=language, key=key):
                    self.assertTrue(value and value == value.strip())
                    for char in "\n\"`$\\":
                        self.assertNotIn(char, value)
                    if key not in quoted:
                        self.assertNotIn(": ", value)
                        self.assertNotIn(" #", value)
                        self.assertNotIn(value[0], "'{}[]&*!|>%@,?:-")

    def test_the_workflows_have_a_single_template(self):
        self.assertEqual(sorted(ASSETS.glob("locales/*/workflows")), [])
        for language in i18n.LANGUAGES:
            self.assertEqual(template_path("workflows/pr-check.yml", language), ASSETS / "workflows/pr-check.yml")

    def test_the_check_name_is_read_from_the_job(self):
        values = template_values(SCRIPTS.parent, ".", "main", Config({"language": "en"}), Git(SCRIPTS.parent))
        check = planned_files(values, selftest=False)[PR_CHECK]
        self.assertTrue(check.startswith("name: PR check\n"))
        self.assertEqual(check_name_in(check), "PR check")
        self.assertIsNone(check_name_in("name: x\njobs:\n  other:\n    name: y\n"))


class TemplateLookupTest(unittest.TestCase):
    def test_the_language_folder_comes_first_and_assets_is_the_fallback(self):
        with tempfile.TemporaryDirectory() as folder:
            assets = Path(folder)
            for name, text in {
                "ISSUE_TEMPLATE/1-a.yml": "pt a\n",
                "ISSUE_TEMPLATE/2-b.yml": "pt b\n",
                "locales/en/ISSUE_TEMPLATE/1-a.yml": "en a\n",
                "pull_request_template.md": "pt pr\n",
            }.items():
                (assets / name).parent.mkdir(parents=True, exist_ok=True)
                (assets / name).write_text(text, encoding="utf-8")
            self.assertEqual(template_path("ISSUE_TEMPLATE/1-a.yml", "en", assets).read_text("utf-8"), "en a\n")
            self.assertEqual(template_path("ISSUE_TEMPLATE/2-b.yml", "en", assets).read_text("utf-8"), "pt b\n")
            self.assertEqual(template_path("ISSUE_TEMPLATE/1-a.yml", "pt-BR", assets).read_text("utf-8"), "pt a\n")
            files = planned_files({"LANGUAGE": "en"}, selftest=False, assets=assets)
            self.assertEqual(
                files,
                {
                    ".github/ISSUE_TEMPLATE/1-a.yml": "en a\n",
                    ".github/ISSUE_TEMPLATE/2-b.yml": "pt b\n",
                    ".github/pull_request_template.md": "pt pr\n",
                },
            )

    def test_the_language_key_replaces_the_commented_line(self):
        template = (ASSETS / "dev-workflow.toml").read_text(encoding="utf-8")
        english = with_language(template, "en")
        self.assertIn('\nlanguage = "en"\n', english)
        self.assertNotIn('# language = "pt-BR"', english)
        self.assertEqual(Config({"schema": 1, "language": "en"}).language, "en")
        self.assertEqual(with_language("schema = 1\n\n[pr]\n", "en"), 'schema = 1\nlanguage = "en"\n\n[pr]\n')


@unittest.skipUnless((SCRIPTS.parent / ".github/workflows/pr-check.yml").is_file(), "fora do repositório do skill")
class WithoutTheKeyTest(unittest.TestCase):
    def test_the_generated_files_are_the_ones_committed_before_the_key(self):
        # As cópias em .github/ deste repositório foram geradas antes do idioma: sem a chave, nada muda.
        root = SCRIPTS.parent
        values = template_values(root, ".", "main", Config(), Git(root))
        for name, content in planned_files(values, selftest=True).items():
            with self.subTest(name):
                self.assertEqual(content, (root / name).read_text(encoding="utf-8"))
        manifest = bootstrap.Manifest("1.0.0", ".", {"a": "b"})
        self.assertNotIn('"language"', manifest.text())
        self.assertIn('"language": "en"', bootstrap.Manifest("1.0.0", ".", {}, "en").text())


class InitLanguageTest(unittest.TestCase):
    def test_init_in_english_writes_the_key_and_doctor_passes(self):
        box = _empty_project()
        self.addCleanup(box.close)
        done = box.run("bootstrap.py", "init", "--no-labels", "--language", "en")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn(f"dev-workflow skill {support_version()} copied to {VENDORED}", done.stdout)
        self.assertIn("Next steps:", done.stdout)
        self.assertIn("`PR check`", done.stdout)
        config = box.read(".github/dev-workflow.toml")
        self.assertRegex(config, r'(?m)^language = "en"$')
        check = box.read(PR_CHECK)
        self.assertTrue(check.startswith("name: PR check\n"))
        self.assertEqual(check_name_in(check), "PR check")
        self.assertIn("name: Version and release\n", box.read(".github/workflows/release.yml"))
        self.assertNotIn("__", check)
        self.assertEqual(json.loads(box.read(MANIFEST))["language"], "en")
        _adopt(box)

        doctor = box.run(str(box.work / VENDORED / "scripts/bootstrap.py"), "doctor")
        self.assertEqual(doctor.returncode, 0, doctor.stdout + doctor.stderr)
        self.assertIn("0 error(s)", doctor.stdout)
        self.assertIn("required check `PR check`", doctor.stdout)
        self.assertNotIn("the required check (the pr-check.yml job)", doctor.stdout)

    def test_language_is_checked_before_anything_is_written(self):
        box = _empty_project()
        self.addCleanup(box.close)
        unknown = box.run("bootstrap.py", "init", "--no-labels", "--language", "fr")
        self.assertEqual(unknown.returncode, 1)
        self.assertIn("pt-BR, en", unknown.stderr)
        self.assertFalse((box.work / ".claude").exists())
        self.assertFalse((box.work / MANIFEST).exists())

    def test_an_existing_configuration_refuses_another_language(self):
        box = Sandbox()  # configuração em português, sem a chave
        self.addCleanup(box.close)
        refused = box.run("bootstrap.py", "init", "--no-labels", "--language", "en")
        self.assertEqual(refused.returncode, 1, refused.stdout)
        self.assertIn("pt-BR", refused.stderr)
        self.assertIn("`language`", refused.stderr)
        self.assertFalse((box.work / ".claude").exists())
        self.assertFalse((box.work / MANIFEST).exists())
        same = box.run("bootstrap.py", "init", "--no-labels", "--language", "pt-br")
        self.assertEqual(same.returncode, 0, same.stdout + same.stderr)
        self.assertNotIn("language", json.loads(box.read(MANIFEST)))
        self.assertEqual(check_name_in(box.read(PR_CHECK)), "Verificação do PR")

    def test_dry_run_in_an_empty_repository_writes_nothing(self):
        box = _empty_project()
        self.addCleanup(box.close)
        dry = box.run("bootstrap.py", "init", "--no-labels", "--language", "en", "--dry-run")
        self.assertEqual(dry.returncode, 0, dry.stdout + dry.stderr)
        self.assertIn("[dry-run] .github/workflows/pr-check.yml created", dry.stdout)
        self.assertEqual(box.git("status", "--porcelain"), "")


def support_version():
    return (SCRIPTS.parent / "VERSION").read_text(encoding="utf-8").strip()


class UpdateLanguageTest(unittest.TestCase):
    def _installed(self, *extra):
        box = _empty_project()
        self.addCleanup(box.close)
        done = box.run("bootstrap.py", "init", "--no-labels", *extra)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        _adopt(box)
        return box

    def _switch(self, box, old, new):
        config = box.read(".github/dev-workflow.toml")
        self.assertIn(f'language = "{old}"', config)
        box.write({".github/dev-workflow.toml": config.replace(f'language = "{old}"', f'language = "{new}"')})

    def test_update_keeps_the_check_name_while_the_key_stays(self):
        box = self._installed("--language", "en")
        done = box.run("bootstrap.py", "update")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(check_name_in(box.read(PR_CHECK)), "PR check")
        self.assertNotIn("required check changes", done.stdout)
        self.assertNotIn("workflow language", done.stdout)

    def test_a_language_switch_regenerates_the_templates_and_warns_about_the_check(self):
        box = self._installed("--language", "en")
        self._switch(box, "en", "pt-BR")
        doctor = box.run(str(box.work / VENDORED / "scripts/bootstrap.py"), "doctor")
        self.assertIn(f"PENDENTE  {PR_CHECK} é o modelo em en, mas a configuração pede pt-BR", doctor.stdout)
        self.assertIn("se chama `PR check`, e em pt-BR se chama `Verificação do PR`", doctor.stdout)

        done = box.run("bootstrap.py", "update")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("idioma do fluxo: en → pt-BR", done.stdout)
        self.assertIn(f"{PR_CHECK} atualizado pelo modelo novo", done.stdout)
        self.assertIn("o check obrigatório passa de `PR check` para `Verificação do PR`", done.stdout)
        self.assertEqual(check_name_in(box.read(PR_CHECK)), "Verificação do PR")
        self.assertNotIn("language", json.loads(box.read(MANIFEST)))

        after = box.run(str(box.work / VENDORED / "scripts/bootstrap.py"), "doctor")
        self.assertEqual(after.returncode, 0, after.stdout + after.stderr)
        self.assertIn("0 erro(s)", after.stdout)
        self.assertNotIn("é o modelo em", after.stdout)
        self.assertNotIn("se chama `PR check`", after.stdout)

    def test_without_a_manifest_the_files_of_another_language_are_still_recognized(self):
        box = self._installed("--language", "en")
        box.write({MANIFEST: None})
        self._switch(box, "en", "pt-BR")
        done = box.run("bootstrap.py", "update")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertNotIn("foi personalizado", done.stdout)
        self.assertIn("idioma do fluxo: en → pt-BR", done.stdout)
        self.assertEqual(check_name_in(box.read(PR_CHECK)), "Verificação do PR")

    def test_uninstall_recognizes_the_templates_of_the_other_language(self):
        box = self._installed("--language", "en")
        box.write({MANIFEST: None})
        listed = box.run(str(box.work / VENDORED / "scripts/bootstrap.py"), "uninstall")
        self.assertEqual(listed.returncode, 0, listed.stdout + listed.stderr)
        self.assertIn(f"  remove {PR_CHECK}", listed.stdout)
        self.assertNotIn("(customized)", listed.stdout)


class HooksTest(unittest.TestCase):
    def test_the_commit_msg_hook_speaks_both_languages(self):
        hook = (ASSETS / "hooks/commit-msg").read_text(encoding="utf-8")
        self.assertIn("preciso de Python 3.11 ou mais novo", hook)
        self.assertIn("Python 3.11 or newer is needed", hook)

    @unittest.skipUnless(shutil.which("node"), "node ausente: o hook do Claude Code é opcional")
    def test_the_claude_hook_triggers_in_both_languages(self):
        script = ASSETS / "claude/dev-workflow-hook.mjs"

        def context(prompt):
            completed = subprocess.run(
                ["node", str(script), "prompt"],
                input=json.dumps({"prompt": prompt, "permission_mode": "default"}),
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=True,
            )
            return completed.stdout

        for prompt in (
            "open a PR for this",
            "create an issue for the crash",
            "bump the version",
            "cut a prerelease",
            "plan the migration",
            "add it to the milestone",
            "planeje a migração",
            "publique a pré-release",
        ):
            self.assertIn("dev-workflow", context(prompt), prompt)
        for prompt in ("explain this function", "the plant needs water", "what does the planet API return?"):
            self.assertEqual(context(prompt), "", prompt)


if __name__ == "__main__":
    unittest.main()
