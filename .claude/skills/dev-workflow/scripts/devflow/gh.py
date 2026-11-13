"""Acesso a ``git`` e ao GitHub (pela CLI ``gh``). Só biblioteca padrão.

Toda chamada ao GitHub passa por ``gh api`` (REST), para que exista um único ponto de troca nos
testes: a variável ``DEV_WORKFLOW_GH`` aceita uma lista JSON com o comando que substitui ``gh``.
``DEV_WORKFLOW_OFFLINE=1`` desliga o GitHub: as ferramentas seguem só com o histórico do git.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

GH_ENV = "DEV_WORKFLOW_GH"
OFFLINE_ENV = "DEV_WORKFLOW_OFFLINE"
WRITE_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})


class CommandError(RuntimeError):
    """Um comando externo terminou com erro."""

    def __init__(self, command: Sequence[str], completed: subprocess.CompletedProcess[str]):
        self.command = list(command)
        self.returncode = completed.returncode
        self.stderr = (completed.stderr or "").strip()
        self.stdout = (completed.stdout or "").strip()
        super().__init__(f"{' '.join(self.command[:4])}…: {self.stderr or self.stdout or completed.returncode}")


def run(
    command: Sequence[str], *, cwd: Path | str | None = None, check: bool = True, stdin: str | None = None
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        list(command),
        cwd=str(cwd) if cwd else None,
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if check and completed.returncode != 0:
        raise CommandError(command, completed)
    return completed


def utf8_streams() -> None:
    """Hooks do git e consoles do Windows entregam pipes em cp1252."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


class Git:
    def __init__(self, root: Path | str | None = None):
        self.root = Path(root) if root else None

    def __call__(self, *args: str, check: bool = True) -> str:
        return run(["git", *args], cwd=self.root, check=check).stdout.strip()

    def ok(self, *args: str) -> bool:
        return run(["git", *args], cwd=self.root, check=False).returncode == 0

    def raw(self, *args: str) -> subprocess.CompletedProcess[str]:
        return run(["git", *args], cwd=self.root, check=False)


def gh_command() -> list[str]:
    override = os.environ.get(GH_ENV)
    if not override:
        return ["gh"]
    command = json.loads(override)
    if not isinstance(command, list) or not all(isinstance(part, str) for part in command):
        raise ValueError(f"{GH_ENV} deve ser uma lista JSON de strings.")
    return command


def run_gh(args: Sequence[str], *, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return run([*gh_command(), *args], check=False, stdin=stdin)


def offline() -> bool:
    return os.environ.get(OFFLINE_ENV, "") not in ("", "0", "false")


_REMOTE = re.compile(r"github\.com[:/](?P<slug>[\w.-]+/[\w.-]+?)(?:\.git)?/?$")


def current_repository(git: Git | None = None) -> str | None:
    """``owner/repo``: do ambiente do Actions, do remoto ``origin`` ou do ``gh``."""
    slug = os.environ.get("GITHUB_REPOSITORY")
    if slug:
        return slug
    if git is not None:
        match = _REMOTE.search(git("remote", "get-url", "origin", check=False))
        if match:
            return match.group("slug")
    if offline():
        return None
    completed = run_gh(["repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"])
    return completed.stdout.strip() or None if completed.returncode == 0 else None


class GitHub:
    """Subconjunto da API REST usado pelo fluxo. ``None`` significa 404."""

    def __init__(self, repository: str | None):
        self.repository = repository
        self.enabled = bool(repository) and not offline()

    @property
    def web(self) -> str:
        server = os.environ.get("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
        return f"{server}/{self.repository}" if self.repository else ""

    def api(self, path: str, *, method: str = "GET", body: Any = None, paginate: bool = False) -> Any:
        if not self.enabled:
            return None
        args = ["api", "-X", method, f"repos/{self.repository}/{path}" if path else f"repos/{self.repository}"]
        if paginate:
            args += ["--paginate", "--slurp"]
        stdin = None
        if body is not None:
            args += ["--input", "-"]
            stdin = json.dumps(body)
        completed = run_gh(args, stdin=stdin)
        if completed.returncode != 0:
            if "HTTP 404" in completed.stderr:
                return None
            raise CommandError(["gh", *args], completed)
        if not completed.stdout.strip():
            return {}
        data = json.loads(completed.stdout)
        if paginate and isinstance(data, list):  # --slurp devolve uma lista de páginas
            return [item for page in data for item in (page if isinstance(page, list) else [page])]
        return data

    # --- leitura ---------------------------------------------------------------------------

    def pull(self, number: int) -> dict[str, Any] | None:
        return self.api(f"pulls/{number}")

    def issue(self, number: int) -> dict[str, Any] | None:
        return self.api(f"issues/{number}")

    def commit_pulls(self, sha: str) -> list[dict[str, Any]]:
        return self.api(f"commits/{sha}/pulls") or []

    def pull_commits(self, number: int) -> list[dict[str, Any]]:
        return self.api(f"pulls/{number}/commits?per_page=100", paginate=True) or []

    def comments(self, number: int) -> list[dict[str, Any]]:
        return self.api(f"issues/{number}/comments?per_page=100", paginate=True) or []

    def release_by_tag(self, tag: str) -> dict[str, Any] | None:
        return self.api(f"releases/tags/{tag}")

    def open_pulls_from(self, branch: str) -> list[dict[str, Any]]:
        owner = (self.repository or "/").split("/", 1)[0]
        return self.api(f"pulls?state=open&head={owner}:{branch}") or []

    def milestones(self) -> list[dict[str, Any]]:
        return self.api("milestones?state=all&per_page=100", paginate=True) or []

    # --- escrita ---------------------------------------------------------------------------

    def add_comment(self, number: int, body: str) -> dict[str, Any]:
        return self.api(f"issues/{number}/comments", method="POST", body={"body": body})

    def update_comment(self, comment_id: int, body: str) -> dict[str, Any]:
        return self.api(f"issues/comments/{comment_id}", method="PATCH", body={"body": body})

    def create_release(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.api("releases", method="POST", body=payload)

    def create_pull(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.api("pulls", method="POST", body=payload)

    def update_pull(self, number: int, payload: dict[str, Any]) -> dict[str, Any]:
        return self.api(f"pulls/{number}", method="PATCH", body=payload)

    def create_milestone(self, title: str) -> dict[str, Any]:
        return self.api("milestones", method="POST", body={"title": title})

    def update_milestone(self, number: int, payload: dict[str, Any]) -> dict[str, Any]:
        return self.api(f"milestones/{number}", method="PATCH", body=payload)

    def set_milestone(self, issue: int, milestone: int) -> dict[str, Any]:
        return self.api(f"issues/{issue}", method="PATCH", body={"milestone": milestone})

    def dispatch_workflow(self, workflow: str, ref: str, inputs: dict[str, str]) -> Any:
        return self.api(f"actions/workflows/{workflow}/dispatches", method="POST", body={"ref": ref, "inputs": inputs})


def label_names(item: dict[str, Any] | None) -> set[str]:
    raw = (item or {}).get("labels")
    if not isinstance(raw, list):
        return set()
    return {str(label.get("name")) if isinstance(label, dict) else str(label) for label in raw}


def is_trusted_author(comment: dict[str, Any], trusted_bots: Sequence[str]) -> bool:
    """Quem pode escrever no repositório, ou um dos bots configurados.

    Num repositório público qualquer pessoa comenta; marcadores lidos pelas ferramentas (análise
    de versão, evidências) só valem quando o autor tem permissão de escrita.
    """
    login = str((comment.get("user") or {}).get("login", ""))
    return comment.get("author_association") in WRITE_ASSOCIATIONS or login in trusted_bots
