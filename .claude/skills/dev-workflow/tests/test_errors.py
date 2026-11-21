"""Falhas de operação: mensagem com a causa em vez de traceback, tempo limite e nova tentativa."""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

import support  # noqa: F401  (põe scripts/ no sys.path antes de importar devflow)
from devflow import gh
from devflow.gh import CommandError, GitHub, http_status, is_transient, remote_slug, retry_delays
from support import SandboxCase

FEAT = [("feat: add the export command", None)]


def _completed(code=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(["gh"], code, stdout, stderr)


class RetryTest(unittest.TestCase):
    def setUp(self):
        self.slept = []
        patcher = mock.patch.object(gh, "sleep", self.slept.append)
        patcher.start()
        self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, {"GITHUB_REPOSITORY": "acme/demo"})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("DEV_WORKFLOW_OFFLINE", None)
        os.environ.pop("DEV_WORKFLOW_RETRIES", None)

    def _github(self, *answers):
        calls = []

        def fake(args, stdin=None):
            calls.append(list(args))
            return answers[min(len(calls), len(answers)) - 1]

        patcher = mock.patch.object(gh, "run_gh", fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return GitHub("acme/demo"), calls

    def test_the_status_is_read_in_one_place(self):
        self.assertEqual(http_status("gh: Bad Gateway (HTTP 502)"), 502)
        self.assertIsNone(http_status("erro sem status"))
        self.assertTrue(is_transient(502, ""))
        self.assertTrue(is_transient(429, ""))
        self.assertTrue(is_transient(403, "You have exceeded a secondary rate limit (HTTP 403)"))
        self.assertFalse(is_transient(403, "Resource not accessible by integration (HTTP 403)"))
        self.assertFalse(is_transient(422, ""))
        self.assertEqual(retry_delays(4), [2.0, 4.0, 8.0])
        self.assertEqual(retry_delays(10)[-1], 30.0)

    def test_a_read_that_gets_502_and_then_200_succeeds(self):
        github, calls = self._github(_completed(1, stderr="gh: Bad Gateway (HTTP 502)"), _completed(0, '{"number": 1}'))
        self.assertEqual(github.pull(1), {"number": 1})
        self.assertEqual((len(calls), self.slept), (2, [2.0]))

    def test_after_the_last_attempt_the_error_names_the_status_and_the_route(self):
        github, calls = self._github(_completed(1, stderr="gh: Service Unavailable (HTTP 503)"))
        with self.assertRaises(CommandError) as raised:
            github.pull(7)
        self.assertEqual(len(calls), 4)
        self.assertIn("HTTP 503", str(raised.exception))
        self.assertIn("GET repos/acme/demo/pulls/7", str(raised.exception))
        self.assertIn("4 tentativas", str(raised.exception))

    def test_creating_a_comment_is_not_repeated(self):
        github, calls = self._github(_completed(1, stderr="gh: Bad Gateway (HTTP 502)"))
        with self.assertRaises(CommandError):
            github.add_comment(3, "texto")
        self.assertEqual((len(calls), self.slept), (1, []))

    def test_a_permanent_error_is_not_repeated_and_404_is_none(self):
        github, calls = self._github(_completed(1, stderr="gh: Validation Failed (HTTP 422)"))
        with self.assertRaises(CommandError):
            github.update_pull(3, {"title": "x"})
        self.assertEqual(len(calls), 1)
        github, calls = self._github(_completed(1, stderr="gh: Not Found (HTTP 404)"))
        self.assertIsNone(github.issue(3))

    def test_an_invalid_json_answer_is_a_readable_error(self):
        github, _ = self._github(_completed(0, "<html>oops</html>"))
        with self.assertRaises(CommandError) as raised:
            github.pull(1)
        self.assertIn("não é JSON válido", str(raised.exception))


class TimeoutAndRemoteTest(unittest.TestCase):
    def test_a_call_past_the_timeout_ends_with_a_readable_error(self):
        with mock.patch.dict(os.environ, {"DEV_WORKFLOW_TIMEOUT": "0.5"}), self.assertRaises(CommandError) as raised:
            gh.run([sys.executable, "-c", "import time; time.sleep(10)"])
        self.assertIn("não respondeu em 0.5 s", str(raised.exception))
        self.assertIn("DEV_WORKFLOW_TIMEOUT", str(raised.exception))

    def test_a_missing_gh_says_it_is_needed_and_how_to_go_offline(self):
        with self.assertRaises(CommandError) as raised:
            gh.run(["gh-que-nao-existe-dev-workflow", "api"], check=False)
        message = str(raised.exception)
        self.assertIn("CLI do GitHub é necessária", message)
        self.assertIn("DEV_WORKFLOW_OFFLINE=1", message)

    def test_github_enterprise_remotes_are_recognized_with_gh_host(self):
        with mock.patch.dict(os.environ, {"GH_HOST": "ghe.exemplo.com", "GITHUB_SERVER_URL": ""}):
            self.assertEqual(remote_slug("https://ghe.exemplo.com/o/r.git"), "o/r")
            self.assertEqual(remote_slug("git@ghe.exemplo.com:o/r.git"), "o/r")
            self.assertEqual(remote_slug("https://github.com/acme/demo"), "acme/demo")
        with mock.patch.dict(os.environ, {"GH_HOST": "", "GITHUB_SERVER_URL": ""}):
            self.assertIsNone(remote_slug("https://ghe.exemplo.com/o/r.git"))
            self.assertIsNone(remote_slug("https://github.com.evil.example/o/r.git"))


class CommandFailureTest(SandboxCase):
    """Nenhum comando termina em traceback quando o ``gh`` falha, falta ou devolve lixo."""

    def _fake(self, code):
        script = Path(self.box.base) / "broken_gh.py"
        script.write_text(code, encoding="utf-8")
        return json.dumps([sys.executable, str(script)])

    def assertReadable(self, completed, *fragments):
        self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
        self.assertNotIn("Traceback", completed.stderr)
        self.assertIn("ERRO:", completed.stderr)
        for fragment in fragments:
            self.assertIn(fragment, completed.stderr)

    def test_check_pr_with_a_failing_gh(self):
        broken = self._fake("import sys\nsys.stderr.write('gh: boom (HTTP 401)\\n')\nsys.exit(1)\n")
        self.assertReadable(self.box.run("github_issues.py", "check-pr", "--pr", "1", env={"DEV_WORKFLOW_GH": broken}), "HTTP 401")

    def test_check_pr_without_gh_on_the_path(self):
        missing = json.dumps(["gh-que-nao-existe-dev-workflow"])
        self.assertReadable(
            self.box.run("github_issues.py", "check-pr", "--pr", "1", env={"DEV_WORKFLOW_GH": missing}),
            "DEV_WORKFLOW_OFFLINE=1",
        )

    def test_check_pr_with_invalid_json(self):
        broken = self._fake("print('isto não é json')\n")
        self.assertReadable(
            self.box.run("github_issues.py", "check-pr", "--pr", "1", env={"DEV_WORKFLOW_GH": broken}),
            "não é JSON válido",
        )

    def test_doctor_names_a_version_file_that_is_not_utf8(self):
        (Path(self.box.work) / "pyproject.toml").write_bytes('[project]\n# descrição\nversion = "1.2.0"\n'.encode("latin-1"))
        completed = self.box.run("bootstrap.py", "doctor", env={"DEV_WORKFLOW_OFFLINE": "1"})
        self.assertNotIn("Traceback", completed.stderr)
        self.assertIn("pyproject.toml: não está em UTF-8", completed.stdout + completed.stderr)

    def test_bootstrap_with_a_failing_gh_ends_without_traceback(self):
        broken = self._fake("import sys\nsys.stderr.write('gh: boom (HTTP 500)\\n')\nsys.exit(1)\n")
        completed = self.box.run("bootstrap.py", "doctor", env={"DEV_WORKFLOW_GH": broken, "DEV_WORKFLOW_RETRIES": "1"})
        self.assertNotIn("Traceback", completed.stderr)
        self.assertIn(completed.returncode, (0, 1))

    def test_a_missing_analysis_file_has_its_own_message(self):
        completed = self.box.run("release.py", "run", "--dry-run", "--analysis-file", "nao-existe.json")
        self.assertReadable(completed, "nao-existe.json", "não existe")


class FlakyGitHubTest(SandboxCase):
    def test_a_release_survives_a_passing_502_on_a_read(self):
        box = self.box
        box.merge_pr(10, "feat: add the export command", FEAT, labels=["release"])
        box.update_state(lambda state: state.__setitem__("failures", {"GET pulls/10": [502]}))
        out = self.assertRan(box.release())
        self.assertIn("v1.3.0", out)
        self.assertEqual(box.remote_tags(), ["v1.2.0", "v1.3.0"])
        self.assertEqual(box.state()["failures"]["GET pulls/10"], [])

    def test_a_comment_is_not_repeated_after_a_502(self):
        box = self.box
        box.merge_pr(10, "feat: add the export command", FEAT, labels=["release"])
        box.update_state(lambda state: state.__setitem__("failures", {"POST issues/10/comments": [502]}))
        out = self.assertRan(box.release())  # marcar é complemento: a versão sai e o aviso diz o que faltou
        self.assertIn("não consegui marcar #10", out)
        self.assertIn("HTTP 502 em POST repos/acme/demo/issues/10/comments", out)
        self.assertEqual(box.state()["log"].count("POST issues/10/comments"), 1)
        self.assertEqual(box.remote_tags(), ["v1.2.0", "v1.3.0"])


class RefusedPushTest(SandboxCase):
    def test_a_push_refused_by_branch_protection_stops_in_the_first_round(self):
        box = self.box
        box.merge_pr(10, "feat: add the export command", FEAT, labels=["release"])
        hook = Path(box.remote) / "hooks" / "pre-receive"
        hook.write_text(
            "#!/bin/sh\necho 'remote: error: GH006: Protected branch update failed for refs/heads/main.' >&2\nexit 1\n",
            encoding="utf-8",
        )
        hook.chmod(0o755)
        completed = box.release()
        self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
        self.assertNotIn("recalculando", completed.stdout)
        self.assertNotIn("não estabilizou", completed.stdout + completed.stderr)
        self.assertIn("GH006", completed.stderr)
        self.assertIn('release.mode = "pull-request"', completed.stderr)
        self.assertIn("RELEASE_TOKEN", completed.stderr)
        self.assertEqual(box.remote_tags(), ["v1.2.0"])


if __name__ == "__main__":
    unittest.main()
