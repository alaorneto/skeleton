"""Acesso a ``git`` e ao GitHub (pela CLI ``gh``). Só biblioteca padrão.

Toda chamada ao GitHub passa por ``gh api`` (REST), para que exista um único ponto de troca nos
testes: a variável ``DEV_WORKFLOW_GH`` aceita uma lista JSON com o comando que substitui ``gh``.
``DEV_WORKFLOW_OFFLINE=1`` desliga o GitHub: as ferramentas seguem só com o histórico do git.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

from . import i18n

GH_ENV = "DEV_WORKFLOW_GH"
# Só os testes: arquivo Python cujo ``main(argv)`` faz o papel do ``gh`` no mesmo processo.
GH_MODULE_ENV = "DEV_WORKFLOW_GH_MODULE"
_GH_MODULES: dict[str, ModuleType] = {}
OFFLINE_ENV = "DEV_WORKFLOW_OFFLINE"
TIMEOUT_ENV = "DEV_WORKFLOW_TIMEOUT"
RETRIES_ENV = "DEV_WORKFLOW_RETRIES"
DEFAULT_TIMEOUT = 120.0  # segundos por chamada a git ou gh
DEFAULT_RETRIES = 4  # tentativas de uma chamada à API em falha passageira, contando a primeira
RETRY_DELAY = 2.0  # espera antes da segunda tentativa; dobra a cada uma
RETRY_DELAY_CAP = 30.0
WRITE_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
# Métodos que podem ser repetidos sem efeito duplicado. POST cria (comentário, release, PR): a
# reexecução da ferramenta já confere antes de criar, então uma falha nele sobe na hora.
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "PUT", "PATCH", "DELETE"})
_HTTP_STATUS = re.compile(r"\bHTTP (?P<status>\d{3})\b")
_RATE_LIMITED = re.compile(r"rate limit|abuse detection", re.IGNORECASE)

# Injetável para os testes não dormirem.
sleep = time.sleep


class CommandError(RuntimeError):
    """Um comando externo terminou com erro."""

    def __init__(self, command: Sequence[str], completed: subprocess.CompletedProcess[str], message: str = ""):
        self.command = list(command)
        self.returncode = completed.returncode
        self.stderr = (completed.stderr or "").strip()
        self.stdout = (completed.stdout or "").strip()
        detail = message or self.stderr or self.stdout or i18n.t("gh.exit", code=completed.returncode)
        super().__init__(f"`{' '.join(self.command[:4])}`: {detail}")


def _timeout() -> float:
    raw = os.environ.get(TIMEOUT_ENV, "").strip()
    try:
        value = float(raw) if raw else DEFAULT_TIMEOUT
    except ValueError:
        value = DEFAULT_TIMEOUT
    return value if value > 0 else DEFAULT_TIMEOUT


def _missing_tool(command: Sequence[str]) -> str:
    name = Path(command[0]).name if command else "?"
    if name.lower().startswith("gh"):
        return i18n.t("gh.missing.gh", command=command[0], env=OFFLINE_ENV)
    return i18n.t("gh.missing.tool", command=command[0])


def run(
    command: Sequence[str], *, cwd: Path | str | None = None, check: bool = True, stdin: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Roda um comando com tempo limite (``DEV_WORKFLOW_TIMEOUT``, em segundos).

    Comando ausente e tempo esgotado viram ``CommandError`` com mensagem para quem opera, mesmo com
    ``check=False``: não há saída que o chamador possa interpretar.
    """
    timeout = _timeout()
    try:
        completed = subprocess.run(
            list(command),
            cwd=str(cwd) if cwd else None,
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout,
        )
    except FileNotFoundError:
        failed = subprocess.CompletedProcess(list(command), 127, "", "")
        raise CommandError(command, failed, _missing_tool(command)) from None
    except subprocess.TimeoutExpired:
        failed = subprocess.CompletedProcess(list(command), 124, "", "")
        message = i18n.t("gh.timeout", seconds=f"{timeout:g}", env=TIMEOUT_ENV)
        raise CommandError(command, failed, message) from None
    if check and completed.returncode != 0:
        raise CommandError(command, completed)
    return completed


def http_status(stderr: str) -> int | None:
    """O status HTTP que o ``gh api`` escreve no erro (``gh: Not Found (HTTP 404)``)."""
    match = _HTTP_STATUS.search(stderr or "")
    return int(match.group("status")) if match else None


def is_transient(status: int | None, stderr: str) -> bool:
    """Falha que passa sozinha: 5xx, 429 e o limite secundário (403 com "rate limit")."""
    if status is not None and (status >= 500 or status == 429):
        return True
    return status == 403 and bool(_RATE_LIMITED.search(stderr or ""))


def _retries() -> int:
    raw = os.environ.get(RETRIES_ENV, "").strip()
    return max(1, int(raw)) if raw.isdigit() else DEFAULT_RETRIES


def retry_delays(attempts: int) -> list[float]:
    """Esperas entre as tentativas: crescem em dobro até o teto."""
    return [min(RETRY_DELAY * 2**index, RETRY_DELAY_CAP) for index in range(max(0, attempts - 1))]


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
    try:
        command = json.loads(override)
    except ValueError:
        command = None
    if not isinstance(command, list) or not all(isinstance(part, str) for part in command):
        raise ValueError(i18n.t("gh.env.invalid", env=GH_ENV))
    return command


def _in_process(path: str, args: Sequence[str], stdin: str | None) -> subprocess.CompletedProcess[str]:
    """Chama ``main(argv)`` de um ``gh`` simulado no próprio processo, como se fosse o executável."""
    module = _GH_MODULES.get(path)
    if module is None:
        spec = importlib.util.spec_from_file_location(f"_dev_workflow_gh_{len(_GH_MODULES)}", path)
        if spec is None or spec.loader is None:
            raise ValueError(i18n.t("gh.module.load", env=GH_MODULE_ENV, path=path))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _GH_MODULES[path] = module
    out, err = io.StringIO(), io.StringIO()
    saved = sys.stdin, sys.stdout, sys.stderr
    sys.stdin, sys.stdout, sys.stderr = io.StringIO(stdin or ""), out, err
    try:
        code = module.main(list(args))
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
    finally:
        sys.stdin, sys.stdout, sys.stderr = saved
    return subprocess.CompletedProcess([path, *args], int(code or 0), out.getvalue(), err.getvalue())


def run_gh(args: Sequence[str], *, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    simulated = os.environ.get(GH_MODULE_ENV)
    if simulated:  # só os testes: o `gh` simulado responde sem abrir um processo por chamada
        return _in_process(simulated, args, stdin)
    return run([*gh_command(), *args], check=False, stdin=stdin)


def offline() -> bool:
    return os.environ.get(OFFLINE_ENV, "") not in ("", "0", "false")


def github_hosts() -> list[str]:
    """``github.com`` e o servidor do GitHub Enterprise, quando ``GH_HOST`` ou o Actions o indicam."""
    hosts = ["github.com"]
    for raw in (os.environ.get("GH_HOST", ""), os.environ.get("GITHUB_SERVER_URL", "")):
        host = re.sub(r"^[a-z]+://", "", raw.strip()).split("/", 1)[0].lower()
        if host and host not in hosts:
            hosts.append(host)
    return hosts


def remote_slug(url: str) -> str | None:
    """``owner/repo`` de um remoto HTTPS ou SSH de um dos servidores de ``github_hosts``."""
    hosts = "|".join(re.escape(host) for host in github_hosts())
    pattern = rf"(?:^|[@/])(?:{hosts})(?::\d+)?[:/](?P<slug>[\w.-]+/[\w.-]+?)(?:\.git)?/?$"
    match = re.search(pattern, url.strip(), re.IGNORECASE)
    return match.group("slug") if match else None


def current_repository(git: Git | None = None) -> str | None:
    """``owner/repo``: do ambiente do Actions, do remoto ``origin`` ou do ``gh``."""
    slug = os.environ.get("GITHUB_REPOSITORY")
    if slug:
        return slug
    if git is not None:
        found = remote_slug(git("remote", "get-url", "origin", check=False))
        if found:
            return found
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
        route = f"{method} {args[3]}"
        attempts = _retries() if method.upper() in IDEMPOTENT_METHODS else 1
        delays = retry_delays(attempts)
        for attempt in range(attempts):
            completed = run_gh(args, stdin=stdin)
            if completed.returncode == 0:
                break
            status = http_status(completed.stderr)
            if status == 404:
                return None
            if attempt < attempts - 1 and is_transient(status, completed.stderr):
                sleep(delays[attempt])
                continue
            shown = f"HTTP {status}" if status else i18n.t("gh.exit", code=completed.returncode)
            detail = completed.stderr.strip() or completed.stdout.strip()
            if attempt:
                message = i18n.t("gh.api.failed_after", shown=shown, route=route, count=attempt + 1, detail=detail)
            else:
                message = i18n.t("gh.api.failed", shown=shown, route=route, detail=detail)
            raise CommandError(["gh", *args], completed, message)
        if not completed.stdout.strip():
            return {}
        try:
            data = json.loads(completed.stdout)
        except ValueError:
            message = i18n.t("gh.api.not_json", route=route, body=completed.stdout.strip()[:200])
            raise CommandError(["gh", *args], completed, message) from None
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
