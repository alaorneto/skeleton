"""Configuration, branch creation, installation and portability of the dev-workflow skill (#210)."""

import ast
import io
import json
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "scripts"))

import bootstrap  # noqa: E402
import github_issues  # noqa: E402
import workflow_config  # noqa: E402
from workflow_config import DEFAULT, ConfigError, parse  # noqa: E402

TEMPLATE = tomllib.loads((SKILL / "assets" / "dev-workflow.toml").read_text(encoding="utf-8"))


def template(**changes):
    data = json.loads(json.dumps(TEMPLATE))
    for dotted, value in changes.items():
        *path, last = dotted.split("__")
        target = data
        for key in path:
            target = target.setdefault(key, {})
        if value is None:
            target.pop(last, None)
        else:
            target[last] = value
    return data


def completed(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class ConfigTest(unittest.TestCase):
    def test_template_loads_with_the_current_rules(self):
        self.assertEqual(
            DEFAULT.type_names, ("bug", "feature", "enhancement", "documentation", "refactor", "test", "ci", "chore")
        )
        self.assertEqual(DEFAULT.type_for_commit("feat").name, "feature")
        self.assertEqual(DEFAULT.epic_label, "epic")
        self.assertEqual(DEFAULT.items_section, "Itens")
        self.assertFalse(DEFAULT.versioning.enabled)

    def test_invalid_values_name_the_key(self):
        cases = [
            ({"types__bug__color": "red"}, "types.bug.color"),
            ({"types__bug__level": "huge"}, "types.bug.level"),
            ({"types__bug__commit": ["feat"]}, "types.feature.commit"),
            ({"project__unknown": 1}, "project.unknown"),
            ({"surprise": {}}, ".surprise"),
            ({"pull_request__pending_criteria": "loud"}, "pull_request.pending_criteria"),
            ({"labels__bug": {"color": "000000", "description": "x"}}, "labels.bug"),
            ({"types": {}}, "types"),
            ({"issue__sections": ["ok", ""]}, "issue.sections[1]"),
            ({"versioning__enabled": True}, "versioning.files"),
            (
                {"versioning__files": [{"path": "../x", "search": "{version}", "replace": "{version}"}]},
                "versioning.files[0].path",
            ),
            ({"versioning__files": [{"path": "x", "regex": "(", "replace": "{version}"}]}, "versioning.files[0].regex"),
            (
                {"versioning__files": [{"path": "x", "regex": "v(.+)", "replace": "{version}"}]},
                "versioning.files[0].regex",
            ),
        ]
        for changes, key in cases:
            with self.subTest(key=key):
                with self.assertRaises(ConfigError) as raised:
                    parse(template(**changes))
                self.assertTrue(str(raised.exception).startswith(key), str(raised.exception))

    def test_load_reports_a_missing_or_broken_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            with self.assertRaises(ConfigError) as raised:
                workflow_config.load(root)
            self.assertIn("bootstrap.py init", str(raised.exception))
            (root / ".github").mkdir()
            (root / ".github" / "dev-workflow.toml").write_text("types = [", encoding="utf-8")
            with self.assertRaises(ConfigError) as raised:
                workflow_config.load(root)
            self.assertIn("TOML inválido", str(raised.exception))

    def test_configured_labels_drive_the_pr_rule(self):
        config = parse(template(types={"defect": {"color": "ff0000", "description": "Defeito", "commit": ["fix"]}}))
        fetch = {1: {"labels": [{"name": "defect"}]}, 2: {"labels": [{"name": "bug"}]}}.get
        self.assertEqual(github_issues.validate_pr("Closes #1", "o/r", fetch, config), [])
        self.assertIn("defect", github_issues.validate_pr("Closes #2", "o/r", fetch, config)[0])

    def test_configured_items_section_and_marker(self):
        config = parse(template(issue={"items_section": "Items", "depends_marker": "depends on"}))
        plan = github_issues.parse_epic_items("## Items\n- [ ] #2 A\n- [ ] #3 B — depends on #2\n", "o/r", config)
        self.assertEqual([(item.number, item.blocked_by) for item in plan.items], [(2, ()), (3, (2,))])
        self.assertEqual(github_issues.parse_epic_items("## Itens\n- [ ] #2 A\n", "o/r", config).items, [])


class StartTest(unittest.TestCase):
    def gh(self, *, state="OPEN", labels=("bug",), title="Corrigir o login (SSO) já!"):
        data = {"number": 42, "title": title, "state": state, "labels": [{"name": name} for name in labels]}
        return lambda args: completed(json.dumps(data))

    def test_branch_name_comes_from_the_issue(self):
        self.assertEqual(
            github_issues.branch_for(42, "Corrigir o login (SSO) já!", ["bug", "security"]),
            "fix/42-corrigir-o-login-sso-ja",
        )
        self.assertEqual(github_issues.branch_for(7, "Nova tela", ["feature"]), "feat/7-nova-tela")
        self.assertEqual(github_issues.slugify("!!!"), "issue")
        self.assertLessEqual(len(github_issues.slugify("palavra " * 30)), github_issues.SLUG_LENGTH)
        with self.assertRaises(ConfigError):
            github_issues.branch_for(1, "x", ["bug", "feature"])

    def test_start_switches_from_the_updated_default_branch(self):
        calls = []

        def git(*args):
            calls.append(args)
            return ""

        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(github_issues.start(42, DEFAULT, gh=self.gh(), git=git), 0)
        self.assertEqual(
            calls[1:], [("fetch", "origin", "main"), ("switch", "-c", "fix/42-corrigir-o-login-sso-ja", "origin/main")]
        )
        self.assertIn("fix/42-", out.getvalue())

    def test_start_refuses_dirty_tree_closed_issue_and_epics(self):
        for gh, git_status, message in (
            (self.gh(), " M file", "mudanças"),
            (self.gh(state="CLOSED"), "", "não está aberta"),
            (self.gh(labels=("epic",)), "", "epic"),
            (self.gh(labels=()), "", "label de tipo"),
        ):
            with self.subTest(message=message):
                calls = []

                def git(*args, status=git_status, calls=calls):
                    calls.append(args)
                    return status if args[0] == "status" else ""

                with redirect_stderr(io.StringIO()) as err:
                    self.assertEqual(github_issues.start(42, DEFAULT, gh=gh, git=git), 1)
                self.assertIn(message, err.getvalue())
                self.assertFalse([call for call in calls if call[0] in ("fetch", "switch")])

    def test_exempt_authors_skip_the_pr_check(self):
        environment = {"GITHUB_REPOSITORY": "o/r", "PR_AUTHOR": "dependabot[bot]", "PR_BODY": ""}
        old = {key: os.environ.get(key) for key in environment}
        os.environ.update(environment)
        try:
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(github_issues.check_pr(DEFAULT), 0)
            self.assertIn("isento", out.getvalue())
        finally:
            for key, value in old.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


class BootstrapTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        self.addCleanup(self.directory.cleanup)

    def labels_gh(self, existing=None):
        names = existing if existing is not None else [label.name for label in (*DEFAULT.types, *DEFAULT.extra_labels)]

        def gh(args):
            if args[:2] == ["label", "list"]:
                return completed("\n".join(names))
            if args[:2] == ["auth", "status"]:
                return completed()
            if args[:2] == ["label", "create"]:
                return completed()
            raise AssertionError(args)

        return gh

    def test_init_in_an_empty_repository_then_doctor_passes(self):
        with redirect_stdout(io.StringIO()):
            report = bootstrap.init(self.root, gh=self.labels_gh(), hook="git")
        self.assertFalse([finding for finding in report if finding.state == "falta"], report)
        for relative in (
            ".github/dev-workflow.toml",
            ".github/pull_request_template.md",
            ".github/ISSUE_TEMPLATE/3-bug.yml",
            ".github/workflows/pr-check.yml",
            ".github/workflows/epic-checklist.yml",
        ):
            self.assertTrue((self.root / relative).is_file(), relative)
        workflow = (self.root / ".github/workflows/pr-check.yml").read_text(encoding="utf-8")
        self.assertNotIn("{{SKILL_DIR}}", workflow)
        self.assertIn("/scripts/github_issues.py check-pr", workflow)
        hook = bootstrap.hook_path(self.root).read_text(encoding="utf-8")
        self.assertIn(bootstrap.HOOK_MARKER, hook)
        self.assertEqual(bootstrap.doctor(self.root, gh=self.labels_gh())[0].state, "ok")
        self.assertFalse(
            [finding for finding in bootstrap.doctor(self.root, gh=self.labels_gh()) if finding.state == "falta"]
        )

    def test_init_keeps_existing_files_and_doctor_reports_gaps(self):
        (self.root / ".github").mkdir()
        (self.root / ".github" / "pull_request_template.md").write_text("mine\n", encoding="utf-8")
        with redirect_stdout(io.StringIO()):
            bootstrap.init(self.root, labels=False, hook="none")
        self.assertEqual((self.root / ".github" / "pull_request_template.md").read_text(encoding="utf-8"), "mine\n")
        findings = {
            finding.item: finding for finding in bootstrap.doctor(self.root, gh=self.labels_gh(existing=["bug"]))
        }
        self.assertEqual(findings["hook commit-msg"].state, "falta")
        self.assertEqual(findings["labels"].state, "falta")
        self.assertIn("feature", findings["labels"].detail)
        unavailable = {item.item: item for item in bootstrap.doctor(self.root, gh=lambda args: completed(returncode=1))}
        self.assertEqual(unavailable["labels"].state, "pendente")

    def test_doctor_points_at_an_invalid_key(self):
        (self.root / ".github").mkdir()
        (self.root / ".github" / "dev-workflow.toml").write_text(
            '[project]\ndefault_branch = 3\n[types.bug]\ncolor = "000000"\ndescription = "x"\n', encoding="utf-8"
        )
        findings = bootstrap.doctor(self.root, gh=self.labels_gh())
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].state, "falta")
        self.assertIn("project.default_branch", findings[0].detail)

    def test_installed_hook_refuses_a_commit_without_issue(self):
        if sys.platform == "win32" and not shutil_which("sh"):
            self.skipTest("o hook é um script sh")
        with redirect_stdout(io.StringIO()):
            bootstrap.init(self.root, labels=False, hook="git")
        skill_copy = self.root / bootstrap.skill_dir(self.root)
        if not skill_copy.exists():
            import shutil

            shutil.copytree(SKILL, skill_copy, ignore=shutil.ignore_patterns("__pycache__", "tests"))
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.test",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.test",
        }
        refused = subprocess.run(
            ["git", "commit", "-q", "--allow-empty", "-m", "feat: x"],
            cwd=self.root,
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("referenciar uma issue", refused.stderr)
        accepted = subprocess.run(
            ["git", "commit", "-q", "--allow-empty", "-m", "feat: x\n\nRefs #1"],
            cwd=self.root,
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(accepted.returncode, 0, accepted.stderr)


def shutil_which(name):
    import shutil

    return shutil.which(name)


class PortabilityTest(unittest.TestCase):
    def test_scripts_import_only_the_standard_library_and_each_other(self):
        local = {path.stem for path in (SKILL / "scripts").glob("*.py")}
        allowed = set(sys.stdlib_module_names) | local
        for path in sorted((SKILL / "scripts").glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    names = [node.module]
                for name in names:
                    with self.subTest(script=path.name, module=name):
                        self.assertIn(name.split(".")[0], allowed)


if __name__ == "__main__":
    unittest.main()
