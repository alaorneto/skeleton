"""Modo ``pre-shell`` do hook do Claude Code: tabela de comandos recusados e permitidos.

O hook é lembrete, não barreira: cada linha da tabela diz se o comando é recusado, e um erro
interno nunca bloqueia. Os repositórios são temporários e a configuração global do git fica de
fora, para que um ``core.hooksPath`` da máquina não mude o resultado.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401  (põe scripts/ no sys.path)
from support import SCRIPTS

HOOK = SCRIPTS.parent / "assets" / "claude" / "dev-workflow-hook.mjs"
CONFIG_PATHS = (".github/dev-workflow.toml", "dev-workflow.toml", ".dev-workflow.toml")
DENY, ALLOW = "deny", "allow"

# (comando, resultado esperado); executados no repositório sem a configuração do fluxo.
BASH_CASES = (
    # git commit: a mensagem não é lida como opção
    ('git commit -m "remove -n flag"', ALLOW),
    ("git commit -m 'feat: x' -- -n", ALLOW),
    ('git commit -am "fix: y"', ALLOW),
    ('git commit --message="usa --no-verify só em emergência"', ALLOW),
    ("git commit -F msg.txt", ALLOW),
    ("git commit -C HEAD --amend", ALLOW),
    ("git commit --no-verify --verify -m x", ALLOW),
    ('git commit -m "a;b" && git push', ALLOW),
    ('echo "git commit --no-verify"', ALLOW),
    ("git log --grep commit -n 3", ALLOW),
    ("git -c user.name=x commit -m y", ALLOW),
    ("git -c core.hooksPath=/dev/null log", ALLOW),
    ("git commit -m \"$(cat <<'EOF'\nfix: don't use -n here; ok\n\nRefs #1\nEOF\n)\"", ALLOW),
    ("git commit -m x # --no-verify", ALLOW),
    ("git commit -m x 2>&1 | tail -n 5", ALLOW),
    # git commit: --no-verify, -n e desvio dos hooks
    ('git commit -m "a;b" --no-verify', DENY),
    ("git commit -n -m x", DENY),
    ("git commit -nm x", DENY),
    ("git commit -m x -n", DENY),
    ("git commit --no-verif -m x", DENY),
    ("git add . && git commit -m x --no-verify", DENY),
    ("git status; git commit --no-verify -m x", DENY),
    ("git commit -m x || git commit -n -m y", DENY),
    ("git status\ngit commit -n -m x", DENY),
    ("(git commit -n -m x)", DENY),
    ("/usr/bin/git commit -n -m x", DENY),
    ("git -C . commit --no-verify -m x", DENY),
    ("git -c core.hooksPath=/dev/null commit -m x", DENY),
    ("git -c core.hookspath= commit -m x", DENY),
    ("git --config-env=core.hooksPath=HP commit -m x", DENY),
    # gh pr create / gh pr new: corpo com referência
    ('gh pr create --fill -b "Closes #1"', ALLOW),
    ('gh pr create --title t --body "Closes #1"', ALLOW),
    ('gh pr create -t t -b "Refs #1"', ALLOW),
    ('gh pr create -t t --body="Closes #1"', ALLOW),
    ('gh pr create -t t -b"Closes #1"', ALLOW),
    ("gh pr create -t t --body-file=body.md", ALLOW),
    ("gh pr create -t t --body-file body.md", ALLOW),
    ("gh pr create -t t -F body.md", ALLOW),
    ("cd sub && gh pr create -t t -F body.md", ALLOW),
    ("gh pr create -t t --body-file - <<'EOF'\nCloses #4\nEOF", ALLOW),
    ("printf 'Closes #4\\n' | gh pr create -t t --body-file -", ALLOW),
    ("gh pr create -t t -F - < body.md", ALLOW),
    ('gh pr new -t t -b "Closes #1"', ALLOW),
    ('gh pr list --search "gh pr create --fill"', ALLOW),
    ('gh pr create -t "x;y" -b "Closes #1"; git status', ALLOW),
    # gh pr create / gh pr new: --fill, sem corpo ou corpo sem referência
    ("gh pr create --fill", DENY),
    ("gh pr create --fill-first", DENY),
    ("gh pr create -f -t t", DENY),
    ('gh pr create -t t --body "sem referência"', DENY),
    ('gh pr create --title "Closes #1"', DENY),
    ("gh pr new -t t", DENY),
    ("gh pr new --fill", DENY),
    ("gh pr create -t t --body-file noref.md", DENY),
    ("gh pr create -t t --body-file missing.md", DENY),
    ("gh pr create -t t --body-file - <<'EOF'\nsem referência\nEOF", DENY),
    ('git status && gh pr create -t t -b "x"', DENY),
    ("echo ok | gh pr create -t t -F -", DENY),
)

POWERSHELL_CASES = (
    ("git commit -m 'remove -n flag' ; git status", ALLOW),
    ('gh pr create --title t --body "Closes #1"', ALLOW),
    ("gh pr create --title t --body-file C:\\nao\\existe.md", DENY),
    ("git commit -m 'x' --no-verify", DENY),
    ("& git commit -n -m x", DENY),
    ('git commit -m "uso `"aspas`" e ;" -n', DENY),
)


@unittest.skipUnless(shutil.which("node"), "node ausente: o hook do Claude Code é opcional")
class ClaudeHookPreShellTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name)
        (cls.root / "gitconfig").write_text("", encoding="utf-8")
        cls.env = {
            **os.environ,
            "GIT_CONFIG_GLOBAL": str(cls.root / "gitconfig"),
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        cls.plain = cls._repo(
            "plain",
            {"body.md": "## Issues\nCloses #1\n", "noref.md": "## Issues\nsem referência\n", "sub/body.md": "Refs #2\n"},
        )

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    @classmethod
    def _repo(cls, name, files):
        repo = cls.root / name
        repo.mkdir()
        for relative, content in {"README.md": "demo\n", **files}.items():
            (repo / relative).parent.mkdir(parents=True, exist_ok=True)
            (repo / relative).write_text(content, encoding="utf-8")
        for args in (
            ("init", "-q"),
            ("add", "-A"),
            ("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "chore: init"),
        ):
            subprocess.run(["git", *args], cwd=repo, env=cls.env, check=True, capture_output=True)
        return repo

    def _run(self, payload, raw=None):
        completed = subprocess.run(
            ["node", str(HOOK), "pre-shell"],
            input=raw if raw is not None else json.dumps(payload),
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=self.env,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        if not completed.stdout:
            return ALLOW, ""
        output = json.loads(completed.stdout)["hookSpecificOutput"]
        return output["permissionDecision"], output["permissionDecisionReason"]

    def _decide(self, command, cwd=None, tool="Bash"):
        payload = {"tool_name": tool, "tool_input": {"command": command}, "cwd": str(cwd or self.plain)}
        return self._run(payload)

    def test_bash_commands_table(self):
        for command, expected in BASH_CASES:
            with self.subTest(command=command):
                self.assertEqual(self._decide(command)[0], expected)

    def test_powershell_commands_table(self):
        for command, expected in POWERSHELL_CASES:
            with self.subTest(command=command):
                self.assertEqual(self._decide(command, tool="PowerShell")[0], expected)

    def test_powershell_keeps_backslashes_in_paths(self):
        command = f"gh pr create -t t --body-file {self.plain / 'body.md'}"
        self.assertEqual(self._decide(command, tool="PowerShell")[0], ALLOW)

    def test_bypassing_the_hooks_directory_is_explained(self):
        decision, reason = self._decide("git -c core.hooksPath=/dev/null commit -m x")
        self.assertEqual(decision, DENY)
        self.assertIn("core.hooksPath", reason)

    def test_adoption_is_found_in_each_config_path(self):
        for index, config in enumerate(CONFIG_PATHS):
            with self.subTest(config=config):
                repo = self._repo(f"adopted{index}", {config: "schema = 1\n"})
                decision, reason = self._decide("git commit -m 'feat: x'", repo)
                self.assertEqual(decision, DENY)
                self.assertIn("commit-msg", reason)
                (repo / ".git" / "hooks" / "commit-msg").write_text("#!/bin/sh\n", encoding="utf-8")
                self.assertEqual(self._decide("git commit -m 'feat: x'", repo)[0], ALLOW)

    def test_git_dash_c_points_the_adoption_check_at_that_repository(self):
        repo = self._repo("adopted-elsewhere", {".dev-workflow.toml": "schema = 1\n"})
        self.assertEqual(self._decide(f"git -C '{repo}' commit -m x")[0], DENY)

    def test_a_config_outside_head_does_not_count_as_adoption(self):
        repo = self._repo("not-adopted", {})
        (repo / "dev-workflow.toml").write_text("schema = 1\n", encoding="utf-8")
        self.assertEqual(self._decide("git commit -m x", repo)[0], ALLOW)

    def test_internal_errors_never_block(self):
        self.assertEqual(self._run(None, raw="{not json")[0], ALLOW)
        self.assertEqual(self._run({"tool_input": {"command": ["git", "commit", "-n"]}})[0], ALLOW)
        missing = self.root / "does-not-exist"
        self.assertEqual(self._decide("git commit -m x", missing)[0], ALLOW)
        self.assertEqual(self._decide('git commit -m "unterminated --no-verify', None)[0], ALLOW)


if __name__ == "__main__":
    unittest.main()
