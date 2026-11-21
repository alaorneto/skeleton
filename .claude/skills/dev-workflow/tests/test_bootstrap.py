"""Bootstrap: caminho do skill, manifesto, update atômico, desinstalação e doctor."""

import json
import re
import unittest

import support  # noqa: F401  (põe scripts/ no sys.path antes de importar devflow)
from devflow.bootstrap import (
    HOOK_MARK,
    MANIFEST,
    Manifest,
    ManifestError,
    digest,
    normalize_skill_dir,
    read_manifest,
    render,
)
from support import BASE_CONFIG, SCRIPTS, Sandbox

VENDORED = ".claude/skills/dev-workflow"
LABELS = '\n[labels]\nrelease = "publicar"\nprerelease_prefix = "canal/"\n'


class ManifestTest(unittest.TestCase):
    def test_the_hash_ignores_the_line_endings_of_the_checkout(self):
        self.assertEqual(digest("a\nb\n"), digest("a\r\nb\r\n"))
        self.assertNotEqual(digest("a\n"), digest("b\n"))

    def test_the_manifest_round_trips_and_recognizes_an_unedited_file(self):
        box = Sandbox()
        self.addCleanup(box.close)
        manifest = Manifest("1.2.0", ".agents/skills/dev-workflow", {"x.yml": digest("conteúdo\n")})
        box.write({MANIFEST: manifest.text()})
        loaded = read_manifest(box.work)
        self.assertEqual(loaded, manifest)
        self.assertTrue(loaded.pristine("x.yml", "conteúdo\r\n"))
        self.assertFalse(loaded.pristine("x.yml", "editado\n"))
        self.assertFalse(loaded.pristine("y.yml", "conteúdo\n"))
        self.assertIsNone(read_manifest(box.base))

    def test_a_broken_manifest_is_an_error_not_an_empty_one(self):
        box = Sandbox()
        self.addCleanup(box.close)
        for text in ("{", '{"schema": 99, "skill_dir": "x", "files": {}}', '{"schema": 1, "files": {}}'):
            box.write({MANIFEST: text})
            with self.assertRaises(ManifestError, msg=text):
                read_manifest(box.work)
        box.write({MANIFEST: '{"schema": 1, "version": "1.0.0", "skill_dir": "../fora", "files": {}}'})
        with self.assertRaises(ManifestError):
            read_manifest(box.work)

    def test_the_skill_dir_stays_inside_the_repository(self):
        self.assertEqual(normalize_skill_dir("./.agents/skills/dev-workflow/"), ".agents/skills/dev-workflow")
        self.assertEqual(normalize_skill_dir("vendor\\dev-workflow"), "vendor/dev-workflow")
        for bad in ("", ".", "../fora", "a/../../b", "/abs", "C:/x", ".git/hooks"):
            with self.assertRaises(ValueError, msg=bad):
                normalize_skill_dir(bad)


class BootstrapLifecycleTest(unittest.TestCase):
    def _installed(self, *extra):
        box = Sandbox()
        self.addCleanup(box.close)
        completed = box.run("bootstrap.py", "init", "--no-labels", *extra)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        box.git("add", "-A")
        box.git("commit", "-q", "-m", "chore: adopt the workflow\n\nRefs #1")
        return box

    def _manifest(self, box):
        return json.loads(box.read(MANIFEST))

    def _age(self, box, skill_dir=VENDORED):
        """Faz a cópia instalada parecer uma versão antiga, com um arquivo que a nova não tem."""
        (box.work / skill_dir / "VERSION").write_text("0.9.0\n", encoding="utf-8")
        (box.work / skill_dir / "scripts/devflow/obsolete.py").write_text("", encoding="utf-8")

    def test_init_with_a_skill_dir_points_the_workflows_there_and_update_keeps_it(self):
        skill_dir = ".agents/skills/dev-workflow"
        box = self._installed("--skill-dir", skill_dir)
        self.assertFalse((box.work / ".claude").exists())
        self.assertTrue((box.work / skill_dir / "SKILL.md").is_file())
        release = box.read(".github/workflows/release.yml")
        self.assertIn(f"python3 {skill_dir}/scripts/release.py run", release)
        self.assertNotIn(VENDORED, release)
        self.assertIn(f"{skill_dir}/scripts/github_issues.py", box.read(".git/hooks/commit-msg"))
        manifest = self._manifest(box)
        self.assertEqual(manifest["skill_dir"], skill_dir)
        self.assertEqual(manifest["version"], (SCRIPTS.parent / "VERSION").read_text(encoding="utf-8").strip())
        self.assertEqual(manifest["files"][".github/workflows/release.yml"], digest(release))
        self.assertIn(".github/ISSUE_TEMPLATE/3-bug.yml", manifest["files"])
        self.assertNotIn(".github/dev-workflow.toml", manifest["files"])  # já existia: é do projeto

        doctor = box.run(str(box.work / skill_dir / "scripts/bootstrap.py"), "doctor")
        self.assertIn("0 erro(s)", doctor.stdout, doctor.stdout)
        self.assertIn(f"versionado em {skill_dir}", doctor.stdout)

        self._age(box, skill_dir)
        done = box.run("bootstrap.py", "update")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn(f"em {skill_dir}", done.stdout)
        self.assertIn(f"removido {skill_dir}/scripts/devflow/obsolete.py", done.stdout)
        self.assertFalse((box.work / ".claude").exists())
        self.assertIn(f"python3 {skill_dir}/scripts/release.py run", box.read(".github/workflows/release.yml"))
        self.assertEqual(self._manifest(box)["skill_dir"], skill_dir)

        itself = box.run(str(box.work / skill_dir / "scripts/bootstrap.py"), "update")
        self.assertEqual(itself.returncode, 1)
        self.assertIn("a partir da versão nova", itself.stderr)

    def test_init_refuses_a_skill_dir_outside_the_repository(self):
        box = Sandbox()
        self.addCleanup(box.close)
        for bad in ("../fora", ".", ".git/x"):
            completed = box.run("bootstrap.py", "init", "--no-labels", "--skill-dir", bad)
            self.assertEqual(completed.returncode, 1, bad)
            self.assertIn("caminho do skill", completed.stderr)
        self.assertFalse((box.work / MANIFEST).exists())

    def test_an_unedited_file_is_recognized_after_the_labels_change(self):
        box = self._installed()
        customized = box.read(".github/workflows/release.yml") + "# do projeto\n"
        box.write({".github/workflows/release.yml": customized})
        box.write({".github/dev-workflow.toml": box.read(".github/dev-workflow.toml") + LABELS})
        done = box.run("bootstrap.py", "update")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn(".github/workflows/semver-analysis.yml atualizado pelo modelo novo", done.stdout)
        analysis = box.read(".github/workflows/semver-analysis.yml")
        self.assertIn("labels.*.name, 'publicar') ||", analysis)
        self.assertEqual(self._manifest(box)["files"][".github/workflows/semver-analysis.yml"], digest(analysis))
        # o personalizado continua sendo do projeto, e o hash antigo fica (é o que o fluxo gerou)
        self.assertIn("ATENÇÃO: .github/workflows/release.yml foi personalizado", done.stdout)
        self.assertEqual(box.read(".github/workflows/release.yml"), customized)
        self.assertNotEqual(self._manifest(box)["files"][".github/workflows/release.yml"], digest(customized))

    def test_without_a_manifest_update_falls_back_to_the_old_comparison_and_writes_one(self):
        box = self._installed()
        box.write({MANIFEST: None, ".github/dev-workflow.toml": box.read(".github/dev-workflow.toml") + LABELS})
        done = box.run("bootstrap.py", "update")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        # sem hash, o arquivo com o label antigo parece personalizado (comportamento anterior)
        self.assertIn("ATENÇÃO: .github/workflows/semver-analysis.yml foi personalizado", done.stdout)
        self.assertIn(f"{MANIFEST} gravado", done.stdout)
        manifest = self._manifest(box)
        self.assertEqual(manifest["skill_dir"], VENDORED)
        self.assertEqual(
            manifest["files"][".github/workflows/pr-check.yml"], digest(box.read(".github/workflows/pr-check.yml"))
        )
        self.assertNotIn(".github/workflows/semver-analysis.yml", manifest["files"])

    def test_a_failure_in_the_middle_of_the_update_leaves_everything_as_it_was(self):
        box = self._installed()
        self._age(box)
        box.write({".github/dev-workflow.toml": box.read(".github/dev-workflow.toml") + LABELS})
        analysis = box.read(".github/workflows/semver-analysis.yml")
        manifest = box.read(MANIFEST)
        # Um formulário de issue virou diretório: a gravação dele, depois dos workflows, falha.
        box.write({".github/ISSUE_TEMPLATE/3-bug.yml": None, ".github/ISSUE_TEMPLATE/3-bug.yml/x": "x"})

        failed = box.run("bootstrap.py", "update")
        self.assertEqual(failed.returncode, 1, failed.stdout)
        self.assertIn("nada foi trocado", failed.stderr)
        vendored = box.work / VENDORED
        self.assertEqual((vendored / "VERSION").read_text(encoding="utf-8"), "0.9.0\n")
        self.assertTrue((vendored / "scripts/devflow/obsolete.py").exists())
        self.assertEqual(box.read(".github/workflows/semver-analysis.yml"), analysis)
        self.assertEqual(box.read(MANIFEST), manifest)
        leftovers = [path.name for path in (box.work / ".claude/skills").iterdir()]
        self.assertEqual(leftovers, ["dev-workflow"])
        self.assertEqual(list(box.work.rglob("*.dev-workflow-tmp")), [])

        box.write({".github/ISSUE_TEMPLATE/3-bug.yml/x": None})
        (box.work / ".github/ISSUE_TEMPLATE/3-bug.yml").rmdir()
        done = box.run("bootstrap.py", "update")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertFalse((vendored / "scripts/devflow/obsolete.py").exists())
        self.assertIn("'publicar'", box.read(".github/workflows/semver-analysis.yml"))
        self.assertTrue((box.work / ".github/ISSUE_TEMPLATE/3-bug.yml").is_file())

    def test_update_removes_what_the_new_version_no_longer_generates(self):
        box = self._installed()
        vendored = box.work / VENDORED
        # Um formulário que só a versão antiga tinha, sem manifesto que o registre: vale o modelo antigo.
        (vendored / "assets/ISSUE_TEMPLATE/6-old.yml").write_text("name: velho\n", encoding="utf-8")
        manifest = self._manifest(box)
        manifest["files"][".github/workflows/legacy.yml"] = digest("name: legado\n")
        manifest["files"][".github/workflows/legacy-edited.yml"] = digest("name: legado\n")
        box.write(
            {
                MANIFEST: json.dumps(manifest),
                ".github/ISSUE_TEMPLATE/6-old.yml": "name: velho\n",
                ".github/workflows/legacy.yml": "name: legado\n",
                ".github/workflows/legacy-edited.yml": "name: legado editado\n",
            }
        )
        done = box.run("bootstrap.py", "update")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn(".github/workflows/legacy.yml removido (a versão nova não o gera mais)", done.stdout)
        self.assertIn(".github/ISSUE_TEMPLATE/6-old.yml removido", done.stdout)
        self.assertFalse((box.work / ".github/workflows/legacy.yml").exists())
        self.assertFalse((box.work / ".github/ISSUE_TEMPLATE/6-old.yml").exists())
        self.assertIn("ATENÇÃO: .github/workflows/legacy-edited.yml não é mais gerado", done.stdout)
        self.assertEqual(box.read(".github/workflows/legacy-edited.yml"), "name: legado editado\n")
        files = self._manifest(box)["files"]
        self.assertNotIn(".github/workflows/legacy.yml", files)
        self.assertNotIn(".github/workflows/legacy-edited.yml", files)

    def test_update_renews_the_skill_hook_and_leaves_a_foreign_one_alone(self):
        box = self._installed()
        current = box.read(".git/hooks/commit-msg")
        self.assertIn(HOOK_MARK, current)
        box.write({".git/hooks/commit-msg": current.replace("exit 1\n", "exit 2 # antigo\n")})
        doctor = box.run(str(box.work / VENDORED / "scripts/bootstrap.py"), "doctor")
        self.assertIn("hook commit-msg desatualizado", doctor.stdout)
        done = box.run("bootstrap.py", "update")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("hook commit-msg renovado", done.stdout)
        self.assertEqual(box.read(".git/hooks/commit-msg"), current)

        foreign = '#!/bin/sh\nexec my-linter "$1"\n'
        box.write({".git/hooks/commit-msg": foreign})
        self.assertEqual(box.run("bootstrap.py", "update").returncode, 0)
        self.assertEqual(box.read(".git/hooks/commit-msg"), foreign)
        doctor = box.run(str(box.work / VENDORED / "scripts/bootstrap.py"), "doctor")
        self.assertIn("é de outra origem e não chama github_issues.py", doctor.stdout)

    def test_uninstall_lists_first_and_removes_only_what_was_not_customized(self):
        box = self._installed()
        customized = box.read(".github/workflows/release.yml") + "# do projeto\n"
        box.write(
            {
                ".github/workflows/release.yml": customized,
                ".github/workflows/own.yml": "name: do projeto\n",
                ".claude/settings.json": json.dumps(
                    {
                        "hooks": {
                            "PreToolUse": [
                                {
                                    "hooks": [
                                        {"command": f"node {VENDORED}/assets/claude/dev-workflow-hook.mjs pre-shell"}
                                    ]
                                }
                            ]
                        }
                    }
                ),
            }
        )
        script = str(box.work / VENDORED / "scripts/bootstrap.py")
        listed = box.run(script, "uninstall")
        self.assertEqual(listed.returncode, 0, listed.stdout + listed.stderr)
        self.assertIn(f"sai   {VENDORED}/", listed.stdout)
        self.assertIn("sai   .github/workflows/pr-check.yml", listed.stdout)
        self.assertIn("fica  .github/workflows/release.yml (personalizado)", listed.stdout)
        self.assertIn("fica  .github/dev-workflow.toml (configuração do projeto)", listed.stdout)
        self.assertIn("fica  .claude/settings.json", listed.stdout)
        self.assertIn("rode de novo com --yes", listed.stdout)
        self.assertTrue((box.work / VENDORED).is_dir())
        self.assertTrue((box.work / ".github/workflows/pr-check.yml").is_file())

        done = box.run(script, "uninstall", "--yes")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertFalse((box.work / ".claude/skills").exists())
        self.assertTrue((box.work / ".claude/settings.json").is_file())
        for name in ("pr-check.yml", "pr-merged.yml", "epic-checklist.yml", "semver-analysis.yml"):
            self.assertFalse((box.work / ".github/workflows" / name).exists(), name)
        self.assertFalse((box.work / ".github/ISSUE_TEMPLATE").exists())
        self.assertFalse((box.work / ".github/pull_request_template.md").exists())
        self.assertFalse((box.work / MANIFEST).exists())
        self.assertFalse((box.work / ".git/hooks/commit-msg").exists())
        self.assertEqual(box.read(".github/workflows/release.yml"), customized)
        self.assertEqual(box.read(".github/workflows/own.yml"), "name: do projeto\n")
        self.assertTrue((box.work / ".github/dev-workflow.toml").is_file())

    def test_uninstall_of_the_skill_repository_keeps_the_skill(self):
        from devflow.bootstrap import copy_skill

        box = Sandbox(config="", files={"README.md": "x\n"})
        self.addCleanup(box.close)
        root = box.base / "skill-repo"
        copy_skill(SCRIPTS.parent, root)
        for args in (("init", "-q", "-b", "main"), ("config", "user.email", "d@e.x"), ("config", "user.name", "D")):
            box.git("-C", str(root), *args)
        script = str(root / "scripts" / "bootstrap.py")
        self.assertEqual(box.run(script, "init", "--no-labels", cwd=root).returncode, 0)
        self.assertEqual(json.loads((root / MANIFEST).read_text(encoding="utf-8"))["skill_dir"], ".")
        done = box.run(script, "uninstall", "--yes", cwd=root)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertTrue((root / "SKILL.md").is_file())
        self.assertTrue((root / "scripts/bootstrap.py").is_file())
        self.assertFalse((root / ".github/workflows/release.yml").exists())
        # a configuração foi gerada por este init e não foi editada: sai também
        self.assertFalse((root / ".github/dev-workflow.toml").exists())

    def test_doctor_points_out_workflows_that_do_not_match_the_configuration(self):
        box = self._installed()
        script = str(box.work / VENDORED / "scripts/bootstrap.py")
        clean = box.run(script, "doctor")
        self.assertIn("0 erro(s)", clean.stdout, clean.stdout)
        self.assertIn(f"{MANIFEST}: versão", clean.stdout)
        self.assertIn("hooks do Claude Code não configurados", clean.stdout)

        merged = box.read(".github/workflows/pr-merged.yml").replace(VENDORED, "vendor/outro")
        box.write(
            {
                ".github/dev-workflow.toml": box.read(".github/dev-workflow.toml") + LABELS,
                ".github/workflows/pr-merged.yml": merged,
                ".claude/settings.json": json.dumps(
                    {
                        "hooks": {
                            "UserPromptSubmit": [
                                {"hooks": [{"command": 'node "$CLAUDE_PROJECT_DIR/x/dev-workflow-hook.mjs" prompt'}]}
                            ]
                        }
                    }
                ),
            }
        )
        doctor = box.run(script, "doctor")
        self.assertEqual(doctor.returncode, 1, doctor.stdout)
        self.assertIn(
            ".github/workflows/pr-merged.yml não corresponde à configuração: chama os scripts em vendor/outro",
            doctor.stdout,
        )
        self.assertIn(
            ".github/workflows/semver-analysis.yml não corresponde à configuração: não reage aos labels", doctor.stdout
        )
        self.assertIn("foi gerado pelo skill e não foi editado", doctor.stdout)
        self.assertIn("hook do Claude Code aponta para x/dev-workflow-hook.mjs", doctor.stdout)

        # O update a partir do clone regenera o que não foi editado; o editado continua apontado.
        self.assertEqual(box.run("bootstrap.py", "update").returncode, 0)
        box.write({".claude/settings.json": None})
        again = box.run(script, "doctor")
        self.assertNotIn("semver-analysis.yml não corresponde", again.stdout)
        self.assertIn("pr-merged.yml não corresponde à configuração", again.stdout)


def _jobs(workflow):
    """``{job: bloco}`` do ``jobs:`` de um workflow (dois espaços de recuo por nível, como os modelos)."""
    body = workflow.split("\njobs:\n", 1)[1]
    parts = re.split(r"^  ([\w-]+):\n", body, flags=re.MULTILINE)
    return dict(zip(parts[1::2], parts[2::2], strict=True))


def _steps(job):
    """Os passos de um job, cada um com o texto dele."""
    return re.split(r"^      - ", job.split("\n    steps:\n", 1)[1], flags=re.MULTILINE)[1:]


class WorkflowHardeningTest(unittest.TestCase):
    def _box(self, config=BASE_CONFIG):
        box = Sandbox(config)
        self.addCleanup(box.close)
        completed = box.run("bootstrap.py", "init", "--no-labels")
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        return box

    def test_a_conditional_line_stays_only_with_its_flag(self):
        template = "a: 1\nb: 2 # nota # __IF_X__\nc: 3 # __IF_Y__\n"
        self.assertEqual(render(template, {"IF_X": "1", "IF_Y": ""}), "a: 1\nb: 2 # nota\n")
        self.assertEqual(render("x # __IF_X__", {}), "")

    def test_every_job_has_a_time_limit(self):
        box = self._box()
        for name in ("pr-check.yml", "pr-merged.yml", "release.yml", "epic-checklist.yml", "semver-analysis.yml"):
            jobs = _jobs(box.read(f".github/workflows/{name}"))
            self.assertTrue(jobs, name)
            for job, block in jobs.items():
                self.assertRegex(block, r"(?m)^    timeout-minutes: \d+$", f"{name}: {job}")
        for template in (SCRIPTS.parent / "assets/workflows").glob("*.yml"):
            if template.name != "dev-workflow-selftest.yml":  # de outra issue
                for job, block in _jobs(template.read_text(encoding="utf-8")).items():
                    self.assertIn("timeout-minutes:", block, f"{template.name}: {job}")

    def test_the_analysis_is_recorded_by_a_copy_made_before_the_agent_runs(self):
        analysis = self._box().read(".github/workflows/semver-analysis.yml")
        steps = _steps(_jobs(analysis)["analysis"])
        names = [re.match(r"(?:name: )?(.*)", step).group(1) for step in steps]
        copy = names.index("Guardar os scripts e a configuração antes do agente")
        agent = names.index("Analisar com o Claude Code")
        record = names.index("Registrar a análise no PR")
        self.assertLess(copy, agent)
        self.assertLess(agent, record)
        self.assertIn(f"cp -R {VENDORED}/scripts", steps[copy])
        self.assertIn('chmod -R a-w "$trusted"', steps[copy])
        self.assertRegex(steps[agent], r"--max-turns \d+")
        # o registro não executa nada do workspace: scripts e configuração vêm da cópia, fora dele
        self.assertNotIn(VENDORED, steps[record])
        self.assertIn("working-directory: ${{ runner.temp }}", steps[record])
        self.assertIn('python3 -I "$RUNNER_TEMP/dev-workflow/scripts/release.py" record-analysis', steps[record])
        self.assertIn("DEV_WORKFLOW_CONFIG: ${{ runner.temp }}/dev-workflow/dev-workflow.toml", steps[record])
        self.assertIn('--file "$GITHUB_WORKSPACE/semver-analysis.json"', steps[record])

    def test_actions_write_only_comes_with_a_dispatch_workflow(self):
        box = self._box()
        self.assertNotIn("actions: write", box.read(".github/workflows/release.yml"))
        # Configurado depois, o update regenera o release.yml não editado (pelo manifesto).
        dispatch = '\n[release]\ndispatch_workflow = "publish.yml"\n'
        box.write({".github/dev-workflow.toml": box.read(".github/dev-workflow.toml") + dispatch})
        doctor = box.run(str(box.work / VENDORED / "scripts/bootstrap.py"), "doctor")
        self.assertIn("release.dispatch_workflow (publish.yml) precisa de `actions: write`", doctor.stdout)
        self.assertEqual(doctor.returncode, 1)
        self.assertEqual(box.run("bootstrap.py", "update").returncode, 0)
        release = box.read(".github/workflows/release.yml")
        self.assertRegex(release, r"(?m)^  actions: write # dispara release\.dispatch_workflow")
        self.assertNotIn("__IF_", release)

        fresh = self._box(BASE_CONFIG + dispatch)
        self.assertIn("  actions: write", fresh.read(".github/workflows/release.yml"))

    def test_the_pr_check_reports_success_in_the_merge_queue_without_redoing_it(self):
        check = self._box().read(".github/workflows/pr-check.yml")
        self.assertRegex(check, r"(?m)^  merge_group:$")
        self.assertIn("group: pr-check-${{ github.event.pull_request.number || github.ref }}", check)
        jobs = _jobs(check)
        self.assertIn("name: Verificação do PR", jobs["pr-check"])  # o mesmo nome do check obrigatório
        first, *verification = _steps(jobs["pr-check"])
        self.assertIn("if: github.event_name == 'merge_group'", first)
        self.assertRegex(first, r"run: echo ")
        for step in verification:
            self.assertRegex(step, r"if: (always\(\) && )?github\.event_name == 'pull_request'", step[:80])
        self.assertIn("if: github.event_name == 'pull_request' &&", jobs["version-preview"])


if __name__ == "__main__":
    unittest.main()
