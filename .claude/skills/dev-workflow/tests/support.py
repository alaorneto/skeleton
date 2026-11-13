"""Apoio dos testes do skill: caminho dos scripts, repositório git temporário e GitHub simulado.

Tudo aqui usa só a biblioteca padrão e ``git``. O repositório temporário fica fora do projeto,
com um remoto *bare* local, para que commit, push e tag aconteçam de verdade.
"""

import contextlib
import inspect
import io
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
SCRIPTS = SKILL / "scripts"
FAKE_GH = Path(__file__).resolve().parent / "fake_gh.py"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

REPOSITORY = "acme/demo"

BASE_CONFIG = """\
schema = 1

[versioning]
initial_version = "1.2.0"

[[versioning.files]]
path = "pyproject.toml"
search = 'version = "{current_pep440}"'

[[versioning.files]]
path = "package.json"
search = '"version": "{current}"'

[[versioning.public_surfaces]]
name = "API pública"
globs = ["api/**"]
"""

BASE_FILES = {
    "pyproject.toml": '[project]\nname = "demo"\nversion = "1.2.0"\n',
    "package.json": '{\n  "name": "demo",\n  "version": "1.2.0"\n}\n',
    "api/client.py": "def fetch():\n    return 1\n",
}

PR_BODY = """\
## Issues
{issues}

## Resumo
- {summary}

## Testes executados
- `make test` — 12 passaram

## Documentação
- Não se aplica — sem mudança de uso

## Versão
- {version}
"""


class Capture:
    """Equivalente mínimo do ``capsys`` do pytest para as funções de teste migradas."""

    def __init__(self):
        self.buffer = io.StringIO()

    def readouterr(self):
        value = self.buffer.getvalue()
        self.buffer.seek(0)
        self.buffer.truncate()
        return types.SimpleNamespace(out=value)


def function_suite(namespace):
    """Suíte ``unittest`` com as funções ``test_*`` de um módulo; ``capsys`` vira ``Capture``."""
    suite = unittest.TestSuite()
    for name, function in sorted(namespace.items()):
        if not name.startswith("test_") or not inspect.isfunction(function):
            continue

        def runner(function=function):
            capture = Capture()
            with contextlib.redirect_stdout(capture.buffer), contextlib.redirect_stderr(io.StringIO()):
                function(capture) if "capsys" in inspect.signature(function).parameters else function()

        suite.addTest(unittest.FunctionTestCase(runner, description=name))
    return suite


class Sandbox:
    """Projeto de mentira com git de verdade e ``gh`` simulado."""

    def __init__(self, config=BASE_CONFIG, files=None, *, tag=None, mode=None):
        self._tmp = tempfile.TemporaryDirectory(prefix="dev-workflow-", ignore_cleanup_errors=True)
        self.base = Path(self._tmp.name)
        self.work = self.base / "work"
        self.remote = self.base / "remote.git"
        self.state_path = self.base / "github.json"
        self.work.mkdir()
        self.write_state(
            {
                "pulls": {},
                "issues": {},
                "comments": {},
                "releases": {},
                "milestones": [],
                "dispatches": [],
                "actor": {"login": "github-actions[bot]", "association": "NONE"},
            }
        )
        if mode:
            config += f'\n[release]\nmode = "{mode}"\n'
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.remote)], check=True)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Dev")
        self.git("config", "user.email", "dev@example.com")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "tag.gpgsign", "false")
        self.git("config", "core.autocrlf", "false")
        self.git("remote", "add", "origin", str(self.remote))
        self.write({".github/dev-workflow.toml": config, **(BASE_FILES if files is None else files)})
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "chore: start the project")
        if tag:
            self.git("tag", "-a", tag, "-m", tag)
        self.git("push", "-q", "--tags", "origin", "main")

    def close(self):
        self._tmp.cleanup()

    # --- git -------------------------------------------------------------------------------

    def git(self, *args, check=True):
        completed = subprocess.run(
            ["git", *args], cwd=self.work, capture_output=True, text=True, encoding="utf-8", check=False
        )
        if check and completed.returncode != 0:
            raise AssertionError(f"git {' '.join(args)}: {completed.stderr}")
        return completed.stdout.strip()

    def write(self, files):
        for name, content in files.items():
            path = self.work / name
            path.parent.mkdir(parents=True, exist_ok=True)
            if content is None:
                path.unlink()
            else:
                path.write_bytes(content.encode("utf-8"))

    def read(self, name):
        return (self.work / name).read_bytes().decode("utf-8")

    def tags(self):
        return sorted(self.git("tag", "--list").split())

    def remote_tags(self):
        output = subprocess.run(
            ["git", "tag", "--list"], cwd=self.remote, capture_output=True, text=True, check=True
        ).stdout
        return sorted(output.split())

    def log(self, *args):
        return self.git("log", "--format=%s", *args).splitlines()

    # --- GitHub simulado -------------------------------------------------------------------

    def state(self):
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def write_state(self, state):
        self.state_path.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")

    def update_state(self, mutate):
        state = self.state()
        mutate(state)
        self.write_state(state)

    def issue(self, number, title, *labels, body=""):
        issue = {
            "number": number,
            "title": title,
            "body": body,
            "state": "open",
            "labels": [{"name": name} for name in labels],
            "milestone": None,
        }
        self.update_state(lambda state: state["issues"].__setitem__(str(number), issue))

    def comment(self, number, body, *, login="alice", association="OWNER"):
        def mutate(state):
            comments = state["comments"].setdefault(str(number), [])
            comments.append(
                {
                    "id": 5000 + sum(len(items) for items in state["comments"].values()),
                    "body": body,
                    "user": {"login": login},
                    "author_association": association,
                    "created_at": f"2029-01-01T00:00:{len(comments):02d}Z",
                }
            )

        self.update_state(mutate)

    def _commit_all(self, branch, commits, base="main"):
        self.git("switch", "-q", "-c", branch, base)
        for index, (subject, files) in enumerate(commits):
            self.write(files if files is not None else {f"notes/{branch.replace('/', '-')}-{index}.txt": subject})
            self.git("add", "-A")
            self.git("commit", "-q", "-m", subject)
        shas = self.git("rev-list", "--reverse", f"{base}..{branch}").splitlines()
        return [{"sha": sha, "commit": {"message": self.git("log", "-1", "--format=%B", sha)}} for sha in shas]

    def merge_pr(
        self,
        number,
        title,
        commits,
        *,
        labels=(),
        body=None,
        closes=(),
        refs=(),
        author="alice",
        squash=False,
        base="main",
    ):
        """Cria a branch, commita, mescla em ``base`` como o GitHub faria e registra o PR."""
        branch = f"work/{number}"
        commit_list = self._commit_all(branch, commits, base)
        self.git("switch", "-q", base)
        if squash:
            self.git("merge", "-q", "--squash", branch)
            self.git("commit", "-q", "-m", f"{title} (#{number})")
        else:
            self.git("merge", "-q", "--no-ff", branch, "-m", f"Merge pull request #{number} from acme/{branch}")
        self.git("branch", "-q", "-D", branch)
        self.git("push", "-q", "origin", base)
        if body is None:
            issues = "\n".join([*(f"Closes #{n}" for n in closes), *(f"Refs #{n}" for n in refs)]) or "Closes #"
            body = PR_BODY.format(issues=issues, summary=title, version="Sem impacto declarado")
        pull = {
            "number": number,
            "title": title,
            "body": body,
            "state": "closed",
            "merged": True,
            "merged_at": "2030-01-01T00:00:00Z",
            "merged_by": {"login": "alice"},
            "merge_commit_sha": self.git("rev-parse", "HEAD"),
            "labels": [{"name": name} for name in labels],
            "user": {"login": author},
            "head": {"ref": branch},
            "base": {"ref": base},
            "commit_list": commit_list,
            "milestone": None,
        }
        self.update_state(lambda state: state["pulls"].__setitem__(str(number), pull))
        return pull

    def open_pr(self, number, title, commits, *, labels=(), closes=(), body=None):
        """PR ainda aberto: a branch existe, nada foi mesclado. Devolve o nome da branch."""
        branch = f"work/{number}"
        commit_list = self._commit_all(branch, commits)
        self.git("switch", "-q", "main")
        issues = "\n".join(f"Closes #{n}" for n in closes) or "Closes #"
        pull = {
            "number": number,
            "title": title,
            "body": body or PR_BODY.format(issues=issues, summary=title, version="Sem impacto declarado"),
            "state": "open",
            "merged": False,
            "merged_at": None,
            "labels": [{"name": name} for name in labels],
            "user": {"login": "alice"},
            "head": {"ref": branch},
            "base": {"ref": "main"},
            "commit_list": commit_list,
            "milestone": None,
        }
        self.update_state(lambda state: state["pulls"].__setitem__(str(number), pull))
        return branch

    # --- execução dos scripts --------------------------------------------------------------

    def env(self, **extra):
        env = {key: value for key, value in os.environ.items() if not key.startswith(("GITHUB_", "RELEASE_"))}
        env.update(
            GITHUB_REPOSITORY=REPOSITORY,
            DEV_WORKFLOW_GH=json.dumps([sys.executable, str(FAKE_GH)]),
            FAKE_GH_STATE=str(self.state_path),
            PYTHONIOENCODING="utf-8",
            PYTHONDONTWRITEBYTECODE="1",
        )
        env.pop("DEV_WORKFLOW_OFFLINE", None)
        env.pop("DEV_WORKFLOW_CONFIG", None)
        env.update(extra)
        return env

    def run(self, script, *args, env=None, cwd=None):
        return subprocess.run(
            [sys.executable, str(SCRIPTS / script), *args],
            cwd=cwd or self.work,
            env=self.env(**(env or {})),
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )

    def release(self, *args, env=None):
        return self.run("release.py", "run", *args, env=env)


class SandboxCase(unittest.TestCase):
    """Cada teste recebe um ``Sandbox`` novo em ``self.box``."""

    config = BASE_CONFIG
    files = None
    tag = "v1.2.0"
    mode = None

    def setUp(self):
        self.box = Sandbox(self.config, self.files, tag=self.tag, mode=self.mode)
        self.addCleanup(self.box.close)

    def assertRan(self, completed, code=0):
        self.assertEqual(completed.returncode, code, f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}")
        return completed.stdout
