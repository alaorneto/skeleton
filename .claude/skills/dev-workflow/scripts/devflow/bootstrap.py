"""Instalação e conferência do fluxo num projeto (``bootstrap.py <subcomando>``).

- ``init [--skill-dir D] [--language L] [--default-branch B] [--initial-version V] [--force] [--no-labels]
  [--dry-run]``: gera a configuração, os workflows, os modelos de issue e de PR e instala o hook
  ``commit-msg``. Se o skill estiver fora do repositório, copia-o para ``--skill-dir`` (padrão
  ``.claude/skills/dev-workflow``; o CI precisa dos scripts versionados). Arquivos que já existem só
  são trocados com ``--force``. ``--language`` grava a chave ``language`` e gera tudo nesse idioma;
  num projeto que já tem configuração, tem de ser o idioma dela. Grava o manifesto
  (``.github/dev-workflow-manifest.json``).
- ``update [--force] [--dry-run]``: rodado a partir da versão nova do skill (fora do projeto),
  troca a cópia no caminho registrado no manifesto, regenera os workflows e modelos que não foram
  personalizados (no idioma da configuração), remove os que a versão nova deixou de gerar e renova
  o hook. Tudo ou nada: uma falha no meio devolve a cópia e os arquivos anteriores. A configuração
  do projeto nunca é tocada.
- ``uninstall [--yes]``: lista o que sai (o que não foi personalizado) e o que fica; com ``--yes``,
  remove.
- ``install-hook``: instala o hook ``commit-msg`` em ``.git/hooks`` (projetos sem pre-commit).
- ``doctor``: confere a instalação e lista o que falta, inclusive os passos de quem é dono.
- ``version``: a versão do skill.

Idioma: os textos deste módulo vêm do catálogo (``locales/``). Os workflows têm um modelo só, em
``assets/workflows/``, para a lógica e os SHAs das actions não divergirem entre idiomas: os nomes
de exibição do modelo são os do catálogo em pt-BR, e ``localize_workflow`` os troca pelos do idioma
do projeto ao gerar. Os outros modelos são procurados primeiro em ``assets/locales/<idioma>/`` e
depois em ``assets/``.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from . import i18n
from .config import CONFIG_CANDIDATES, Config, ConfigError, find_root, load
from .gh import CommandError, Git, GitHub, current_repository, offline, run_gh, utf8_streams
from .i18n import t, translate
from .plan import Repo
from .release import ReleaseError, check_version_files
from .semver import Version
from .text import find_section, sections

SKILL = Path(__file__).resolve().parents[2]
ASSETS = SKILL / "assets"
VENDOR_PATH = ".claude/skills/dev-workflow"
# O que é do repositório do skill, não do skill: não vai para a cópia dentro de um projeto.
COPY_IGNORE = (
    "__pycache__",
    "*.pyc",
    "*-workspace",
    ".git",
    ".github",
    ".pr-head",
    ".claude",  # configuração local do Claude Code e worktrees de quem desenvolve o skill
    ".ruff_cache",
    ".mypy_cache",
    "ruff.toml",
    "mypy.ini",
    "SECURITY.md",
    "CONTRIBUTING.md",
)
WORKFLOWS = ("pr-check.yml", "pr-merged.yml", "release.yml", "epic-checklist.yml", "semver-analysis.yml")
OPTIONAL_WORKFLOWS = ("dev-workflow-selftest.yml",)
PR_CHECK = ".github/workflows/pr-check.yml"
# O que o fluxo gerou neste projeto: versão, onde está o skill e o hash de cada arquivo gerado.
MANIFEST = ".github/dev-workflow-manifest.json"
MANIFEST_SCHEMA = 1
# Primeira linha de comentário do hook gerado: é o que o distingue de um hook de outra origem.
HOOK_MARK = "Hook commit-msg do skill dev-workflow"
CLAUDE_HOOK = "assets/claude/dev-workflow-hook.mjs"
CLAUDE_SETTINGS = (".claude/settings.json", ".claude/settings.local.json")

# (arquivo, expressão que captura a versão, marcador usado no padrão de busca)
DETECTORS: tuple[tuple[str, str, str], ...] = (
    ("pyproject.toml", r'^version\s*=\s*"(?P<v>[^"]+)"', "{current_pep440}"),
    ("package.json", r'^\s*"version"\s*:\s*"(?P<v>[^"]+)"', "{current}"),
    ("Cargo.toml", r'^version\s*=\s*"(?P<v>[^"]+)"', "{current}"),
    ("composer.json", r'^\s*"version"\s*:\s*"(?P<v>[^"]+)"', "{current}"),
    ("pubspec.yaml", r"^version:\s*(?P<v>\S+)", "{current}"),
    ("mix.exs", r'version:\s*"(?P<v>[^"]+)"', "{current}"),
    ("build.gradle", r"^version\s*=?\s*'(?P<v>[^']+)'", "{current}"),
    ("build.gradle.kts", r'^version\s*=\s*"(?P<v>[^"]+)"', "{current}"),
    ("Directory.Build.props", r"<Version>(?P<v>[^<]+)</Version>", "{current}"),
    ("VERSION", r"^(?P<v>\d+\.\d+\.\d+[^\s]*)\s*$", "{current}"),
    ("version.txt", r"^(?P<v>\d+\.\d+\.\d+[^\s]*)\s*$", "{current}"),
)


def skill_version(skill: Path = SKILL) -> str:
    """Versão do skill (arquivo ``VERSION``); ``"?"`` numa cópia antiga, sem o arquivo."""
    try:
        return (skill / "VERSION").read_text(encoding="utf-8").strip() or "?"
    except OSError:
        return "?"


def is_skill(path: Path) -> bool:
    """O diretório tem este skill (e não outro com o mesmo caminho)?"""
    skill_md = path / "SKILL.md"
    if not skill_md.is_file() or not (path / "scripts" / "bootstrap.py").is_file():
        return False
    head = skill_md.read_text(encoding="utf-8", errors="replace")[:400]
    return re.search(r"^name:\s*dev-workflow\s*$", head, re.MULTILINE) is not None


def _skill_files(base: Path) -> set[str]:
    """Arquivos do skill em ``base``, relativos a ela, sem o que é só do repositório do skill."""
    ignore = shutil.ignore_patterns(*COPY_IGNORE)
    found: set[str] = set()
    for directory, dirs, names in os.walk(base):
        skipped = ignore(directory, [*dirs, *names])
        dirs[:] = [name for name in dirs if name not in skipped]
        here = Path(directory)
        found |= {(here / name).relative_to(base).as_posix() for name in names if name not in skipped}
    return found


def copy_skill(source: Path, destination: Path) -> tuple[list[str], list[str]]:
    """Espelha ``source`` em ``destination``: copia tudo e remove o que saiu. Devolve (copiados, removidos)."""
    wanted = _skill_files(source)
    removed = sorted(_skill_files(destination) - wanted) if destination.exists() else []
    for name in removed:
        (destination / name).unlink()
    for name in sorted(wanted):
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / name, target)
    for directory, _dirs, _names in sorted(os.walk(destination), key=lambda item: -len(Path(item[0]).parts)):
        if Path(directory) != destination and not any(Path(directory).iterdir()):
            Path(directory).rmdir()
    return sorted(wanted), removed


def _normalize(version: str) -> Version | None:
    """Aceita SemVer e a forma PEP 440 das pré-releases (``1.2.0rc1``)."""
    direct = Version.try_parse(version)
    if direct:
        return direct
    match = re.fullmatch(r"(\d+\.\d+\.\d+)(a|b|rc)(\d+)", version)
    names = {"a": "alpha", "b": "beta", "rc": "rc"}
    return Version.try_parse(f"{match[1]}-{names[match[2]]}.{match[3]}") if match else None


def detect_version_files(root: Path) -> tuple[list[dict[str, str]], Version | None]:
    """Arquivos de versão reconhecidos na raiz e a versão que eles declaram."""
    entries: list[dict[str, str]] = []
    found: Version | None = None
    for name, pattern, token in DETECTORS:
        path = root / name
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        match = re.search(pattern, text, re.MULTILINE)
        version = _normalize(match.group("v")) if match else None
        if not match or version is None:
            continue
        literal = match.group(0).strip()
        entry = {"path": name, "search": literal.replace(match.group("v"), token)}
        occurrences = text.count(literal)
        if occurrences != 1:
            entry["note"] = t("bootstrap.version_files.repeated", literal=literal, count=occurrences)
        entries.append(entry)
        found = found or version
    return entries, found


def _toml_string(value: str) -> str:
    return f"'{value}'" if "'" not in value else json.dumps(value, ensure_ascii=False)


def render_version_files(entries: list[dict[str, str]]) -> str:
    if not entries:
        return t("bootstrap.version_files.none")
    blocks = []
    for entry in entries:
        note = f"# {entry['note']}\n" if entry.get("note") else ""
        blocks.append(
            f'{note}[[versioning.files]]\npath = "{entry["path"]}"\nsearch = {_toml_string(entry["search"])}\n'
        )
    return "\n".join(blocks)


# Linha condicional de um modelo: ``conteúdo  # __IF_NOME__`` só fica se ``values["IF_NOME"]`` não é vazio.
_CONDITIONAL = re.compile(r"^(?P<line>.*?)[ \t]*# __IF_(?P<flag>\w+)__[ \t]*(?:\n|\Z)", re.MULTILINE)


def render(template: str, values: dict[str, str]) -> str:
    template = _CONDITIONAL.sub(
        lambda match: match["line"] + "\n" if values.get(f"IF_{match['flag']}") else "", template
    )
    for key, value in values.items():
        template = template.replace(f"__{key}__", value)
    return template


def default_branch(git: Git) -> str:
    head = git("symbolic-ref", "--short", "refs/remotes/origin/HEAD", check=False)
    if head.startswith("origin/"):
        return head[len("origin/") :]
    return git("symbolic-ref", "--short", "HEAD", check=False) or "main"


def skill_dir_in(root: Path) -> str | None:
    """Caminho do skill relativo à raiz do repositório, ou ``None`` se estiver fora dela."""
    try:
        return SKILL.relative_to(root.resolve()).as_posix()
    except ValueError:
        return None


# --- idioma dos modelos ----------------------------------------------------------------------------


def template_path(relative: str, language: str, assets: Path = ASSETS) -> Path:
    """O modelo em ``assets/locales/<idioma>/`` e, se o idioma não tiver um, em ``assets/``.

    Os modelos em português ficam onde sempre estiveram (``assets/**`` é superfície pública); uma
    cópia antiga do skill, sem ``locales/``, cai sempre neles.
    """
    localized = assets / "locales" / language / relative
    return localized if localized.is_file() else assets / relative


def _template_names(directory: str, language: str, assets: Path) -> list[str]:
    """Nomes dos ``*.yml`` de um diretório de modelos, somando o do idioma e o padrão."""
    folders = (assets / directory, assets / "locales" / language / directory)
    return sorted({path.name for folder in folders for path in folder.glob("*.yml")})


def workflow_texts(language: str, skill_dir: str) -> dict[str, str]:
    """Nomes de exibição dos workflows e as mensagens que eles escrevem, num idioma.

    Os modelos de ``assets/workflows/`` têm estes textos em pt-BR (o teste confere que são iguais aos
    do catálogo); ``localize_workflow`` os troca pelos do idioma do projeto. O nome do job de
    ``pr-check.yml`` é o check obrigatório da proteção de branch: o teste fixa o valor de cada idioma.
    """
    return {
        "workflow.pr_check.job": required_check(language),
        "workflow.pr_check.queue": translate(language, "workflow.pr_check.queue"),
        "workflow.pr_check.queue_echo": translate(language, "workflow.pr_check.queue_echo"),
        "workflow.pr_check.issue": translate(language, "workflow.pr_check.issue"),
        "workflow.pr_check.no_skill": translate(language, "workflow.pr_check.no_skill"),
        "workflow.pr_check.files": translate(language, "workflow.pr_check.files"),
        "workflow.pr_check.request": translate(language, "workflow.pr_check.request"),
        "workflow.pr_check.no_request": translate(language, "workflow.pr_check.no_request"),
        "workflow.preview.job": translate(language, "workflow.preview.job"),
        "workflow.preview.step": translate(language, "workflow.preview.step"),
        "workflow.preview.no_skill": translate(language, "workflow.preview.no_skill"),
        "workflow.merged.name": translate(language, "workflow.merged.name"),
        "workflow.merged.job": translate(language, "workflow.merged.job"),
        "workflow.merged.step": translate(language, "workflow.merged.step"),
        "workflow.release.name": translate(language, "workflow.release.name"),
        "workflow.release.input.force": translate(language, "workflow.release.input.force"),
        "workflow.release.input.level": translate(language, "workflow.release.input.level"),
        "workflow.release.input.reason": translate(language, "workflow.release.input.reason"),
        "workflow.release.input.prerelease": translate(language, "workflow.release.input.prerelease"),
        "workflow.release.input.dry_run": translate(language, "workflow.release.input.dry_run"),
        "workflow.release.job": translate(language, "workflow.release.job"),
        "workflow.release.identity": translate(language, "workflow.release.identity"),
        "workflow.release.run": translate(language, "workflow.release.run"),
        "workflow.epic.name": translate(language, "workflow.epic.name"),
        "workflow.epic.job": translate(language, "workflow.epic.job"),
        "workflow.epic.sync": translate(language, "workflow.epic.sync"),
        "workflow.epic.close": translate(language, "workflow.epic.close"),
        "workflow.analysis.name": translate(language, "workflow.analysis.name"),
        "workflow.analysis.job": translate(language, "workflow.analysis.job"),
        "workflow.analysis.plan": translate(language, "workflow.analysis.plan"),
        "workflow.analysis.trusted": translate(language, "workflow.analysis.trusted"),
        "workflow.analysis.agent": translate(language, "workflow.analysis.agent"),
        "workflow.analysis.prompt": translate(language, "workflow.analysis.prompt", skill_dir=skill_dir),
        "workflow.analysis.record": translate(language, "workflow.analysis.record"),
        "workflow.analysis.cleanup": translate(language, "workflow.analysis.cleanup"),
        "workflow.selftest.name": translate(language, "workflow.selftest.name"),
        "workflow.selftest.job": translate(language, "workflow.selftest.job"),
        "workflow.selftest.step": translate(language, "workflow.selftest.step"),
    }


# Onde um texto de exibição aparece num workflow: `name:` (do workflow, do job ou do passo, às vezes
# seguido de ` (${{ matrix.os }})`), `description:` de uma entrada manual e `echo` de uma mensagem.
_DISPLAY_NAME = r"(?m)^(?P<lead>[ \t]*(?:- )?name: ){text}(?=[ \t]*$| \()"
_DISPLAY_QUOTED = ('description: "{text}"', 'echo "{text}"')
# O prompt do agente é um bloco literal do YAML: as linhas levam o recuo do bloco.
_PROMPT_INDENT = " " * 12


def localize_workflow(text: str, language: str, skill_dir: str) -> str:
    """Um workflow gerado com os textos de exibição em ``language``; a lógica não muda.

    Só as posições de exibição são trocadas: comentários e comandos ficam como no modelo, então a
    lógica e os SHAs das actions são os mesmos em todos os idiomas.
    """
    if language == i18n.DEFAULT_LANGUAGE:
        return text
    source, target = workflow_texts(i18n.DEFAULT_LANGUAGE, skill_dir), workflow_texts(language, skill_dir)
    for key in sorted(source, key=lambda name: -len(source[name])):  # o texto mais longo primeiro
        original, wanted = source[key], target[key]
        if key == "workflow.analysis.prompt":
            indent = "\n" + _PROMPT_INDENT
            before, after = (_PROMPT_INDENT + value.replace("\n", indent) for value in (original, wanted))
            text = text.replace(before, after)
            continue
        def swap(match: re.Match[str], wanted: str = wanted) -> str:
            return match["lead"] + wanted

        text = re.sub(_DISPLAY_NAME.format(text=re.escape(original)), swap, text)
        for form in _DISPLAY_QUOTED:
            text = text.replace(form.format(text=original), form.format(text=wanted))
    return text


def required_check(language: str) -> str:
    """O nome do check obrigatório (o job de ``pr-check.yml``) num idioma."""
    return translate(language, "workflow.pr_check.job")


_CHECK_JOB = re.compile(r"^  pr-check:[ \t]*\n(?:[ \t]*#.*\n)*    name:[ \t]*(?P<name>.+?)[ \t]*$", re.MULTILINE)


def check_name_in(text: str | None) -> str | None:
    """O nome do job ``pr-check`` (o check obrigatório) no texto de um ``pr-check.yml``."""
    match = _CHECK_JOB.search(text or "")
    return match["name"].strip("'\"") if match else None


def language_of_check(name: str | None) -> str | None:
    """O idioma cujo check obrigatório tem este nome."""
    return next((language for language in i18n.LANGUAGES if name and required_check(language) == name), None)


def template_values(
    root: Path, skill_dir: str, branch: str, cfg: Config | None, git: Git, language: str | None = None
) -> dict[str, str]:
    repository = current_repository(git) or "OWNER/REPO"
    language = language or (cfg.language if cfg else i18n.DEFAULT_LANGUAGE)
    return {
        "SKILL_DIR": skill_dir,
        "SKILL_GLOB": "**" if skill_dir == "." else f"{skill_dir}/**",
        "SKILL_PATH": "" if skill_dir == "." else f"{skill_dir}/",
        "DEFAULT_BRANCH": branch,
        "RELEASE_LABEL": cfg.label("release") if cfg else "release",
        "PRERELEASE_PREFIX": cfg.label("prerelease_prefix") if cfg else "release:",
        "REPOSITORY_URL": f"https://github.com/{repository}",
        # `actions: write` no release.yml só quando há workflow de publicação a disparar.
        "IF_DISPATCH": "1" if cfg and cfg.release["dispatch_workflow"] else "",
        "LANGUAGE": language,
    }


def planned_files(values: dict[str, str], *, selftest: bool, assets: Path = ASSETS) -> dict[str, str]:
    """``{caminho no projeto: conteúdo}`` dos modelos, sem a configuração, no idioma de ``values``."""
    language = values.get("LANGUAGE", i18n.DEFAULT_LANGUAGE)
    files: dict[str, str] = {}
    for name in (*WORKFLOWS, *(OPTIONAL_WORKFLOWS if selftest else ())):
        template = template_path(f"workflows/{name}", language, assets)
        if template.is_file():
            rendered = render(template.read_text(encoding="utf-8"), values)
            files[f".github/workflows/{name}"] = localize_workflow(rendered, language, values.get("SKILL_DIR", "."))
    for name in _template_names("ISSUE_TEMPLATE", language, assets):
        template = template_path(f"ISSUE_TEMPLATE/{name}", language, assets)
        files[f".github/ISSUE_TEMPLATE/{name}"] = render(template.read_text(encoding="utf-8"), values)
    pull_request = template_path("pull_request_template.md", language, assets)
    if pull_request.is_file():
        files[".github/pull_request_template.md"] = pull_request.read_text(encoding="utf-8")
    return files


def planned_by_language(
    root: Path, skill_dir: str, branch: str, cfg: Config | None, git: Git, *, selftest: bool, assets: Path = ASSETS
) -> dict[str, dict[str, str]]:
    """``planned_files`` em cada idioma: um arquivo igual a qualquer um deles é um modelo não editado."""
    return {
        language: planned_files(
            template_values(root, skill_dir, branch, cfg, git, language), selftest=selftest, assets=assets
        )
        for language in i18n.LANGUAGES
    }


_LANGUAGE_LINE = re.compile(r"^#?[ \t]*language[ \t]*=.*$", re.MULTILINE)


def with_language(config: str, language: str) -> str:
    """A configuração gerada com a chave ``language`` ativa (troca a linha comentada do modelo)."""
    line = f'language = "{language}"'
    if _LANGUAGE_LINE.search(config):
        return _LANGUAGE_LINE.sub(line, config, count=1)
    return re.sub(r"^schema = 1[ \t]*$", f"schema = 1\n{line}", config, count=1, flags=re.MULTILINE)


# --- manifesto ---------------------------------------------------------------------------------


class ManifestError(ValueError):
    """Manifesto ilegível ou de outro formato."""


@dataclass
class Manifest:
    """O que uma instalação gerou: com ele, arquivo gerado e não editado é reconhecido pelo hash,
    mesmo depois de a configuração (label, branch) mudar o que o modelo renderiza.

    ``language`` é o idioma em que os arquivos foram gerados; só é gravado quando não é o padrão
    (os manifestos de antes da chave são todos em pt-BR).
    """

    version: str
    skill_dir: str
    files: dict[str, str] = field(default_factory=dict)
    language: str = i18n.DEFAULT_LANGUAGE

    def pristine(self, name: str, text: str) -> bool:
        return self.files.get(name) == digest(text)

    def text(self) -> str:
        data = {
            "about": t("bootstrap.manifest.about"),
            "schema": MANIFEST_SCHEMA,
            "version": self.version,
            "skill_dir": self.skill_dir,
            **({"language": self.language} if self.language != i18n.DEFAULT_LANGUAGE else {}),
            "files": dict(sorted(self.files.items())),
        }
        return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def digest(text: str) -> str:
    """sha256 do conteúdo com fim de linha LF: um checkout com ``autocrlf`` não parece editado."""
    return hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def read_manifest(root: Path) -> Manifest | None:
    path = root / MANIFEST
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ManifestError(t("bootstrap.manifest.unreadable", manifest=MANIFEST, error=exc)) from exc
    files = data.get("files") if isinstance(data, dict) else None
    if (
        not isinstance(files, dict)
        or data.get("schema") != MANIFEST_SCHEMA
        or not isinstance(data.get("skill_dir"), str)
        or not all(isinstance(key, str) and isinstance(value, str) for key, value in files.items())
    ):
        raise ManifestError(t("bootstrap.manifest.format", manifest=MANIFEST, schema=MANIFEST_SCHEMA))
    try:
        skill_dir = "." if data["skill_dir"] == "." else normalize_skill_dir(data["skill_dir"])
    except ValueError as exc:
        raise ManifestError(f"{MANIFEST}: {exc}") from exc
    language = i18n.normalize(data.get("language", i18n.DEFAULT_LANGUAGE))
    if language is None:
        accepted = ", ".join(i18n.LANGUAGES)
        raise ManifestError(
            t("bootstrap.manifest.language", manifest=MANIFEST, value=data.get("language"), accepted=accepted)
        )
    return Manifest(str(data.get("version", "?")), skill_dir, dict(files), language)


def normalize_skill_dir(value: str) -> str:
    """Caminho do skill relativo à raiz, com ``/``; recusa o que sairia do repositório."""
    raw = value.strip().replace("\\", "/")
    path = PurePosixPath(raw)
    if not raw or path.is_absolute() or re.match(r"^[A-Za-z]:", raw) or ".." in path.parts:
        raise ValueError(t("bootstrap.skill_dir.outside", value=repr(value)))
    normalized = path.as_posix()
    if normalized in ("", ".") or path.parts[0] == ".git":
        raise ValueError(t("bootstrap.skill_dir.root", value=repr(value)))
    return normalized


def installed_skill_dir(root: Path, manifest: Manifest | None) -> str:
    """Onde está o skill do projeto: este mesmo, se roda de dentro; senão o do manifesto ou o padrão."""
    inside = skill_dir_in(root)
    if inside is not None:
        return inside
    return manifest.skill_dir if manifest else VENDOR_PATH


class Transaction:
    """Grava tudo ou nada: guarda o estado anterior de cada caminho e o devolve se algo falhar."""

    def __init__(self) -> None:
        self._undo: list[Callable[[], None]] = []
        self._leftovers: list[Path] = []

    def write(self, path: Path, content: str, *, executable: bool = False) -> None:
        previous = path.read_bytes() if path.is_file() else None
        missing = [parent for parent in reversed(path.parents) if not parent.exists()]
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.dev-workflow-tmp")
        temporary.write_bytes(content.encode("utf-8"))
        try:
            os.replace(temporary, path)
        except OSError:
            temporary.unlink(missing_ok=True)
            for parent in reversed(missing):
                _rmdir_if_empty(parent)
            raise
        if executable:
            path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        def undo() -> None:
            if previous is None:
                path.unlink(missing_ok=True)
                for parent in reversed(missing):
                    _rmdir_if_empty(parent)
            else:
                path.write_bytes(previous)

        self._undo.append(undo)

    def remove(self, path: Path) -> None:
        previous = path.read_bytes()
        path.unlink()

        def restore() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(previous)

        self._undo.append(restore)

    def swap_dir(self, staged: Path, destination: Path) -> None:
        """Põe ``staged`` no lugar de ``destination``; a anterior fica de reserva até o fim."""
        backup = destination.with_name(f".{destination.name}.update-old")
        _rmtree(backup)
        os.rename(destination, backup)
        try:
            os.rename(staged, destination)
        except OSError:
            os.rename(backup, destination)
            raise

        def undo() -> None:
            _rmtree(destination)
            os.rename(backup, destination)

        self._undo.append(undo)
        self._leftovers.append(backup)

    def rollback(self) -> list[str]:
        """Desfaz na ordem inversa; devolve o que não conseguiu desfazer."""
        failures = []
        for undo in reversed(self._undo):
            try:
                undo()
            except OSError as exc:
                failures.append(str(exc))
        self._undo.clear()
        return failures

    def commit(self) -> None:
        for path in self._leftovers:
            _rmtree(path)
        self._undo.clear()


def _rmtree(path: Path) -> None:
    def retry(function, name, _exc):  # arquivo somente leitura no Windows
        os.chmod(name, stat.S_IWRITE)
        function(name)

    if path.exists():
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=retry)
        else:
            shutil.rmtree(path, onerror=retry)


def _rmdir_if_empty(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.rmdir()


def _prune_empty(path: Path, root: Path) -> None:
    """Remove os diretórios que ficaram vazios, de ``path`` até a raiz (exclusive)."""
    root = root.resolve()
    current = path.resolve()
    while current != root and root in current.parents:
        if not current.is_dir() or any(current.iterdir()):
            return
        current.rmdir()
        current = current.parent


# --- hook commit-msg -----------------------------------------------------------------------------


def hook_path(git: Git) -> Path:
    path = Path(git("rev-parse", "--git-path", "hooks/commit-msg"))
    return path if path.is_absolute() else (git.root or Path.cwd()) / path


def hook_content(skill_dir: str, assets: Path = ASSETS) -> str:
    return render((assets / "hooks" / "commit-msg").read_text(encoding="utf-8"), {"SKILL_DIR": skill_dir})


def install_hook(root: Path, skill_dir: str, *, force: bool = False) -> str:
    git = Git(root)
    target = hook_path(git)
    content = hook_content(skill_dir)
    if target.exists() and not force:
        existing = target.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")
        if existing == content:
            return t("bootstrap.hook.present", path=target)
        if HOOK_MARK in existing:
            pass  # o hook deste skill, de outra versão ou de outro caminho: é renovado
        elif "github_issues.py" in existing or "pre-commit" in existing:
            return t("bootstrap.hook.present", path=target)
        else:
            return t("bootstrap.hook.foreign", path=target)
    renewed = target.exists()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content.encode("utf-8"))
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return t("bootstrap.hook.renewed", path=target) if renewed else t("bootstrap.hook.installed", path=target)


def init(args: argparse.Namespace) -> int:
    root = find_root()
    git = Git(root)
    requested: str | None = None
    if args.language:
        requested = i18n.normalize(args.language)
        if requested is None:
            accepted = ", ".join(i18n.LANGUAGES)
            return _refuse(t("bootstrap.init.language_unknown", value=args.language, accepted=accepted))
        i18n.activate(requested)
    if not (root / ".git").exists():
        return _refuse(t("bootstrap.init.not_git"))
    try:
        wanted = normalize_skill_dir(args.skill_dir) if args.skill_dir else None
        manifest = read_manifest(root)
    except ValueError as exc:
        return _refuse(str(exc))
    # A configuração que fica decide o idioma (e os labels dos modelos); --language não a contradiz.
    existing_config = next((name for name in CONFIG_CANDIDATES if (root / name).is_file()), None)
    cfg: Config | None = None
    config_warning = None
    kept_language = None
    if existing_config is not None:
        try:
            loaded = load(root)
        except ConfigError as exc:
            loaded = None
            if not args.force:
                config_warning = t("bootstrap.init.config_invalid", error=exc)
        if loaded is not None:
            kept_language = loaded.language
            cfg = None if args.force else loaded
    if cfg is not None and requested is not None and requested != cfg.language:
        return _refuse(
            t("bootstrap.init.language_conflict", config=existing_config, current=cfg.language, requested=requested)
        )
    language = requested or kept_language or i18n.DEFAULT_LANGUAGE
    i18n.activate(language)
    skill_dir = skill_dir_in(root)
    actions: list[str] = []
    if skill_dir is not None and wanted is not None and wanted != skill_dir:
        return _refuse(t("bootstrap.init.inside", skill_dir=skill_dir))
    if skill_dir is None:
        skill_dir = wanted or (manifest.skill_dir if manifest else VENDOR_PATH)
        destination = root / skill_dir
        if destination.exists() and not args.force:
            return _refuse(t("bootstrap.init.exists", skill_dir=skill_dir))
        if not args.dry_run:
            copy_skill(SKILL, destination)
        actions.append(t("bootstrap.init.copied", version=skill_version(), skill_dir=skill_dir))
    branch = args.default_branch or default_branch(git)
    entries, detected = detect_version_files(root)
    initial = args.initial_version or (str(detected) if detected else "0.1.0")
    if Version.try_parse(initial) is None:
        return _refuse(t("bootstrap.init.not_semver", version=initial))
    if config_warning:
        actions.append(config_warning)
    values = template_values(
        root, skill_dir, cfg.default_branch if cfg and not args.default_branch else branch, cfg, git, language
    )
    files = planned_files(values, selftest=args.selftest)
    if existing_config is None or args.force:
        config_values = {**values, "INITIAL_VERSION": initial, "VERSION_FILES": render_version_files(entries)}
        config = render(
            template_path("dev-workflow.toml", language)
            .read_text(encoding="utf-8")
            .replace("# __VERSION_FILES__", "__VERSION_FILES__"),
            config_values,
        )
        if requested is not None or language != i18n.DEFAULT_LANGUAGE:
            config = with_language(config, language)
        files = {CONFIG_CANDIDATES[0]: config, **files}
    else:
        actions.append(t("bootstrap.init.config_kept", name=existing_config))
    # O manifesto registra o que é do fluxo: o que este init gravou e o que já estava igual ao modelo.
    recorded = Manifest(skill_version(), skill_dir, dict(manifest.files) if manifest else {}, language)
    for name, content in files.items():
        target = root / name
        if target.exists() and not args.force:
            same = target.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n") == content
            if same:
                recorded.files[name] = digest(content)
            actions.append(t("bootstrap.init.same", name=name) if same else t("bootstrap.init.kept", name=name))
            continue
        if not args.dry_run:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content.encode("utf-8"))
        recorded.files[name] = digest(content)
        actions.append(t("bootstrap.init.created", name=name))
    if not args.dry_run:
        (root / MANIFEST).parent.mkdir(parents=True, exist_ok=True)
        (root / MANIFEST).write_bytes(recorded.text().encode("utf-8"))
    actions.append(t("bootstrap.init.manifest", manifest=MANIFEST))
    if (root / ".pre-commit-config.yaml").is_file():
        actions.append(t("bootstrap.init.pre_commit", skill_dir=skill_dir))
    elif not args.dry_run:
        actions.append(install_hook(root, skill_dir, force=args.force))
    if not args.no_labels and not args.dry_run and not offline():
        from .issues import sync_labels  # noqa: PLC0415

        try:
            ok = sync_labels(load(root)) == 0
        except (ConfigError, OSError) as exc:
            ok = False
            actions.append(f"labels: {exc}")
        actions.append(t("bootstrap.init.labels_created") if ok else t("bootstrap.init.labels_missing"))
    prefix = "[dry-run] " if args.dry_run else ""
    for action in actions:
        print(f"{prefix}{action}")
    print(t("bootstrap.init.next", skill_dir=skill_dir, check=required_check(language)))
    return 0


# --- update ------------------------------------------------------------------------------------


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").replace("\r\n", "\n")
    except OSError:
        return None


def _refuse(message: str) -> int:
    print(t("bootstrap.error", message=message), file=sys.stderr)
    return 1


def update(args: argparse.Namespace) -> int:
    """Troca a cópia do skill no projeto pela versão que está rodando este comando."""
    root = find_root()
    if skill_dir_in(root) is not None:
        return _refuse(t("bootstrap.update.inside"))
    try:
        manifest = read_manifest(root)
    except ManifestError as exc:
        return _refuse(str(exc))
    skill_dir = manifest.skill_dir if manifest else VENDOR_PATH
    destination = root / skill_dir
    if not is_skill(destination):
        return _refuse(t("bootstrap.update.not_skill", skill_dir=skill_dir))
    old, new = skill_version(destination), skill_version()
    old_version, new_version = Version.try_parse(old), Version.try_parse(new)
    if new_version is None:
        return _refuse(t("bootstrap.update.not_semver", version=new))
    if old_version is not None and new_version.key() < old_version.key() and not args.force:
        return _refuse(t("bootstrap.update.newer", old=old, new=new))
    try:
        cfg = load(root)
    except ConfigError as exc:
        return _refuse(str(exc))
    git = Git(root)

    # O idioma é o da configuração: o update nunca o troca sozinho. Os arquivos gerados num idioma
    # (registrado no manifesto ou, sem ele, reconhecido pelo nome do check) passam para o da
    # configuração só quando a chave `language` mudou.
    language = cfg.language
    current_check = check_name_in(_read(root / PR_CHECK))
    if manifest is not None:
        previous_language = manifest.language
    else:
        previous_language = language_of_check(current_check) or i18n.DEFAULT_LANGUAGE

    # Arquivo não personalizado recebe o modelo novo: o hash bate com o do manifesto ou, sem ele, o
    # conteúdo é o modelo antigo, em qualquer idioma, com os valores de hoje. Arquivo personalizado
    # só é trocado com --force. Opcional ausente continua ausente.
    values = template_values(root, skill_dir, cfg.default_branch, cfg, git, language)
    before = planned_by_language(
        root, skill_dir, cfg.default_branch, cfg, git, selftest=True, assets=destination / "assets"
    )
    after = planned_files(values, selftest=True)
    recorded = manifest.files if manifest else {}
    renewed = Manifest(new, skill_dir, dict(recorded), language)
    optional = {f".github/workflows/{name}" for name in (*OPTIONAL_WORKFLOWS, "semver-analysis.yml")}

    def pristine(name: str, text: str) -> bool:
        return recorded.get(name) == digest(text) or any(text == files.get(name) for files in before.values())

    regenerate: dict[str, str] = {}
    customized: list[str] = []
    for name, content in after.items():
        current = _read(root / name)
        if current == content:
            renewed.files[name] = digest(content)
        elif current is None and name in optional:
            renewed.files.pop(name, None)
        elif current is None or pristine(name, current) or args.force:
            regenerate[name] = content
            renewed.files[name] = digest(content)
        else:
            customized.append(name)
    # O que a versão nova deixou de gerar: sai se não foi editado; se foi, fica e deixa de ser do fluxo.
    obsolete: list[str] = []
    orphaned: list[str] = []
    generated_before = set().union(*before.values())
    for name in sorted((set(recorded) | generated_before) - set(after) - set(CONFIG_CANDIDATES)):
        renewed.files.pop(name, None)
        current = _read(root / name)
        if current is not None:
            (obsolete if pristine(name, current) else orphaned).append(name)
    new_check = check_name_in(regenerate.get(PR_CHECK))
    renamed_check = current_check is not None and new_check is not None and new_check != current_check

    # O hook deste checkout: renovado se é o do skill (o de outra origem não é tocado).
    hook = hook_path(git)
    current_hook = _read(hook)
    new_hook = hook_content(skill_dir)
    renew_hook = current_hook is not None and current_hook != new_hook and HOOK_MARK in current_hook

    prefix = "[dry-run] " if args.dry_run else ""
    wanted = _skill_files(SKILL)
    removed = sorted(_skill_files(destination) - wanted)
    if args.dry_run:
        print(f"{prefix}{t('bootstrap.update.dry_run', old=old, new=new, skill_dir=skill_dir)}")
    else:
        staged = destination.with_name(f".{destination.name}.update-new")
        _rmtree(staged)
        _rmtree(destination.with_name(f".{destination.name}.update-old"))
        transaction = Transaction()
        try:
            # Tudo o que pode falhar por espaço ou permissão acontece antes de tocar no projeto.
            copy_skill(SKILL, staged)
            transaction.swap_dir(staged, destination)
            for name, content in regenerate.items():
                transaction.write(root / name, content)
            for name in obsolete:
                transaction.remove(root / name)
            transaction.write(root / MANIFEST, renewed.text())
            if renew_hook:
                transaction.write(hook, new_hook, executable=True)
        except OSError as exc:
            failures = transaction.rollback()
            _rmtree(staged)
            if failures:
                return _refuse(t("bootstrap.update.failed_partial", error=exc, failures="; ".join(failures)))
            return _refuse(t("bootstrap.update.failed", error=exc, old=old))
        transaction.commit()
        for name in obsolete:
            _prune_empty((root / name).parent, root)
        print(
            t("bootstrap.update.done", old=old, new=new, skill_dir=skill_dir, count=len(wanted), removed=len(removed))
        )
        for name in removed:
            print(t("bootstrap.update.removed_file", skill_dir=skill_dir, name=name))
    if previous_language != language:
        print(f"{prefix}{t('bootstrap.update.language', old=previous_language, new=language)}")
    for name in regenerate:
        print(f"{prefix}{t('bootstrap.update.regenerated', name=name)}")
    for name in obsolete:
        print(f"{prefix}{t('bootstrap.update.obsolete', name=name)}")
    for name in customized:
        print(f"{prefix}{t('bootstrap.update.customized', name=name)}")
    for name in orphaned:
        print(f"{prefix}{t('bootstrap.update.orphaned', name=name)}")
    if renamed_check:
        print(f"{prefix}{t('bootstrap.update.check_renamed', old=current_check, new=new_check)}")
    if renew_hook:
        print(f"{prefix}{t('bootstrap.hook.renewed', path=hook)}")
    if manifest is None:
        print(f"{prefix}{t('bootstrap.update.manifest_written', manifest=MANIFEST)}")
    else:
        print(f"{prefix}{t('bootstrap.update.manifest_updated', manifest=MANIFEST)}")
    print(f"{prefix}{t('bootstrap.update.config_kept', name=CONFIG_CANDIDATES[0])}")
    print(t("bootstrap.update.next", skill_dir=skill_dir))
    return 0


# --- uninstall ---------------------------------------------------------------------------------


def _claude_hook_files(root: Path) -> list[str]:
    """Arquivos de configuração do Claude Code que chamam o hook do skill."""
    return [name for name in CLAUDE_SETTINGS if "dev-workflow-hook.mjs" in (_read(root / name) or "")]


def uninstall(args: argparse.Namespace) -> int:
    """Remove o que o fluxo gerou e não foi personalizado; lista o resto. Sem ``--yes``, só lista."""
    root = find_root()
    git = Git(root)
    try:
        manifest = read_manifest(root)
        cfg = load(root)
    except (ManifestError, ConfigError) as exc:
        return _refuse(str(exc))
    skill_dir = installed_skill_dir(root, manifest)
    copy = root / skill_dir
    # O skill na raiz do repositório é o próprio projeto: só os arquivos gerados saem.
    remove_copy = skill_dir != "." and is_skill(copy)
    assets = (copy if is_skill(copy) else SKILL) / "assets"
    planned = planned_by_language(root, skill_dir, cfg.default_branch, cfg, git, selftest=True, assets=assets)
    names = set().union(*planned.values())
    recorded = manifest.files if manifest else {}
    config = cfg.path.relative_to(root).as_posix() if cfg.path and cfg.path.is_relative_to(root) else None

    remove: list[str] = []
    keep: list[str] = []
    for name in sorted(set(recorded) | names | ({config} if config else set())):
        current = _read(root / name)
        if current is None:
            continue
        if recorded.get(name) == digest(current) or any(current == files.get(name) for files in planned.values()):
            remove.append(name)
        else:
            keep.append(
                t("bootstrap.uninstall.customized", name=name)
                if name != config
                else t("bootstrap.uninstall.config", name=name)
            )
    hook = hook_path(git)
    hook_text = _read(hook)
    remove_hook = hook_text is not None and hook_text in (hook_content(skill_dir, assets), hook_content(skill_dir))
    if hook_text is not None and not remove_hook and HOOK_MARK in hook_text:
        keep.append(t("bootstrap.uninstall.hook_edited", path=hook))
    if (root / ".pre-commit-config.yaml").is_file() and "github_issues.py" in (
        _read(root / ".pre-commit-config.yaml") or ""
    ):
        keep.append(t("bootstrap.uninstall.pre_commit"))
    keep += [t("bootstrap.uninstall.claude", name=name) for name in _claude_hook_files(root)]
    keep.append(t("bootstrap.uninstall.history"))

    print(t("bootstrap.uninstall.title", root=root))
    if remove_copy:
        print(t("bootstrap.uninstall.copy", skill_dir=skill_dir, version=skill_version(copy)))
    for name in remove:
        print(t("bootstrap.uninstall.leaves", item=name))
    if remove_hook:
        print(t("bootstrap.uninstall.leaves", item=hook))
    if (root / MANIFEST).is_file():
        print(t("bootstrap.uninstall.leaves", item=MANIFEST))
    for item in keep:
        print(t("bootstrap.uninstall.stays", item=item))
    if not args.yes:
        print(t("bootstrap.uninstall.nothing"))
        return 0

    for name in remove:
        (root / name).unlink()
        _prune_empty((root / name).parent, root)
    if remove_hook:
        hook.unlink()
    (root / MANIFEST).unlink(missing_ok=True)
    _prune_empty((root / MANIFEST).parent, root)
    if remove_copy:  # por último: pode ser a cópia que está rodando este comando
        _rmtree(copy)
        _prune_empty(copy.parent, root)
    print(t("bootstrap.uninstall.done"))
    return 0


# --- doctor ------------------------------------------------------------------------------------


class Report:
    def __init__(self) -> None:
        self.errors = 0
        self.pending = 0

    @staticmethod
    def _line(tag: str, message: str) -> None:
        print(f"  {tag:<10}{message}")

    def ok(self, message: str) -> None:
        self._line(t("bootstrap.doctor.tag.ok"), message)

    def todo(self, message: str) -> None:
        self.pending += 1
        self._line(t("bootstrap.doctor.tag.pending"), message)

    def error(self, message: str) -> None:
        self.errors += 1
        self._line(t("bootstrap.doctor.tag.error"), message)


_SCRIPT_CALL = re.compile(r"""(?:python3?\s+|script=")([^\s"'$]+)/scripts/(?:release|github_issues)\.py""")


def workflow_problems(name: str, text: str, cfg: Config, skill_dir: str) -> list[str]:
    """O que no workflow contradiz a configuração ou o lugar do skill (vale para os personalizados)."""
    problems = []
    paths = sorted({match.group(1) for match in _SCRIPT_CALL.finditer(text)} - {skill_dir})
    if paths:
        problems.append(t("bootstrap.doctor.wrong_scripts", paths=", ".join(paths), skill_dir=skill_dir))
    if name.endswith("/release.yml"):
        match = re.search(r"^\s*branches:\s*\[(?P<items>[^\]]*)\]", text, re.MULTILINE)
        branches = [item.strip().strip("'\"") for item in match.group("items").split(",")] if match else []
        if match and cfg.default_branch not in branches:
            problems.append(
                t("bootstrap.doctor.wrong_branch", branch=cfg.default_branch, branches=", ".join(branches))
            )
    dispatch = cfg.release["dispatch_workflow"]
    if name.endswith("/release.yml") and dispatch and not re.search(r"^\s*actions:\s*write\b", text, re.MULTILINE):
        problems.append(t("bootstrap.doctor.dispatch", workflow=dispatch))
    if name.endswith("/semver-analysis.yml"):
        release, prefix = cfg.label("release"), cfg.label("prerelease_prefix")
        if f"'{release}'" not in text or f"',{prefix}'" not in text:
            problems.append(t("bootstrap.doctor.labels_condition", release=release, prefix=prefix))
    return problems


def hook_python() -> str | None:
    """O primeiro Python 3.11+ que o hook commit-msg acharia, na mesma ordem que ele tenta."""
    for candidate in (["python3"], ["python"], ["py", "-3"]):
        executable = shutil.which(candidate[0])
        if executable is None:
            continue
        try:
            completed = subprocess.run(
                [executable, *candidate[1:], "-c", "import sys; sys.exit(sys.version_info < (3, 11))"],
                capture_output=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if completed.returncode == 0:
            return " ".join(candidate)
    return None


def hook_report(report: Report, hook: Path, skill_dir: str) -> None:
    text = _read(hook)
    if text is None:
        report.todo(t("bootstrap.doctor.hook_missing"))
        return
    if text == hook_content(skill_dir):
        report.ok(t("bootstrap.doctor.hook_ok"))
    elif HOOK_MARK in text:
        report.todo(t("bootstrap.doctor.hook_stale"))
    elif "github_issues.py" in text or "pre-commit" in text:
        report.ok(t("bootstrap.doctor.hook_other"))
        return
    else:
        report.todo(t("bootstrap.doctor.hook_foreign", path=hook))
        return
    found = hook_python()
    if found:
        report.ok(t("bootstrap.doctor.hook_python", python=found))
    else:
        report.error(t("bootstrap.doctor.hook_no_python"))


_CLAUDE_HOOK_PATH = re.compile(r"""(?:\$\{?CLAUDE_PROJECT_DIR\}?/)?([^\s"']*dev-workflow-hook\.mjs)""")


def claude_hooks_report(report: Report, root: Path, skill_dir: str) -> None:
    """Hooks do Claude Code: opcionais, mas se configurados precisam apontar para o skill e ter node."""
    expected = CLAUDE_HOOK if skill_dir == "." else f"{skill_dir}/{CLAUDE_HOOK}"
    configured = False
    for name in CLAUDE_SETTINGS:
        text = _read(root / name)
        if text is None:
            continue
        try:
            data = json.loads(text)
        except ValueError as exc:
            report.error(t("bootstrap.doctor.claude_json", name=name, error=exc))
            continue
        hooks = data.get("hooks") if isinstance(data, dict) else None
        commands = [
            str(hook.get("command", ""))
            for groups in (hooks.values() if isinstance(hooks, dict) else [])
            if isinstance(groups, list)
            for group in groups
            if isinstance(group, dict) and isinstance(group.get("hooks"), list)
            for hook in group["hooks"]
            if isinstance(hook, dict)
        ]
        paths = {match.group(1) for command in commands for match in _CLAUDE_HOOK_PATH.finditer(command)}
        configured = configured or bool(paths)
        for path in sorted(paths):
            if path != expected:
                report.error(t("bootstrap.doctor.claude_path", name=name, path=path, expected=expected))
        if paths and not (root / expected).is_file():
            report.error(t("bootstrap.doctor.claude_missing", name=name, expected=expected))
        elif paths and expected in paths:
            report.ok(t("bootstrap.doctor.claude_ok", name=name))
    if not configured:
        report.ok(t("bootstrap.doctor.claude_none"))
    elif shutil.which("node") is None:
        report.todo(t("bootstrap.doctor.claude_node"))


def language_report(report: Report, root: Path, cfg: Config, manifest: Manifest | None) -> None:
    """O idioma dos arquivos gerados e o check obrigatório, que muda de nome com ele."""
    expected = required_check(cfg.language)
    found = check_name_in(_read(root / PR_CHECK))
    if found is not None and found != expected:
        report.todo(t("bootstrap.doctor.check_name", found=found, expected=expected, language=cfg.language))
    elif manifest is not None and manifest.language != cfg.language:
        recorded, language = manifest.language, cfg.language
        report.todo(t("bootstrap.doctor.manifest_language", manifest=MANIFEST, recorded=recorded, language=language))


def protection_report(report: Report, github: GitHub, branch: str, check: str) -> None:
    """O check obrigatório na proteção da branch, quando o token pode ler a proteção (dono ou admin)."""
    try:
        data = github.api(f"branches/{branch}/protection/required_status_checks")
    except CommandError:
        return  # sem permissão de leitura da proteção: fica o lembrete do fim do doctor
    if not isinstance(data, dict):
        return
    contexts = {str(item) for item in data.get("contexts") or []}
    contexts |= {str(item.get("context")) for item in data.get("checks") or [] if isinstance(item, dict)}
    if not contexts:
        return
    if check in contexts:
        report.ok(t("bootstrap.doctor.protection_ok", branch=branch, check=check))
    else:
        shown = ", ".join(f"`{name}`" for name in sorted(contexts))
        report.todo(t("bootstrap.doctor.protection_missing", branch=branch, checks=shown, check=check))


def doctor(_args: argparse.Namespace) -> int:
    root = find_root()
    git = Git(root)
    report = Report()
    print(t("bootstrap.doctor.title", root=root))
    try:
        cfg = load(root)
    except ConfigError as exc:
        report.error(str(exc))
        return 1
    if cfg.path is None:
        report.error(t("bootstrap.doctor.no_config"))
    else:
        report.ok(t("bootstrap.doctor.config", path=cfg.path.relative_to(root).as_posix()))
    # Abaixo de 3.11 o bootstrap.py nem chega aqui: recusa antes de importar (tomllib).
    report.ok(t("bootstrap.doctor.python", version=f"{sys.version_info.major}.{sys.version_info.minor}"))
    try:
        manifest = read_manifest(root)
    except ManifestError as exc:
        report.error(str(exc))
        manifest = None
    skill_dir = skill_dir_in(root)
    if skill_dir is None:
        skill_dir = manifest.skill_dir if manifest else VENDOR_PATH
        report.error(t("bootstrap.doctor.outside", skill_dir=skill_dir))
    else:
        report.ok(t("bootstrap.doctor.versioned", version=skill_version(), skill_dir=skill_dir))
    if manifest is None:
        report.ok(t("bootstrap.doctor.no_manifest", manifest=MANIFEST))
    elif manifest.skill_dir != skill_dir:
        recorded = manifest.skill_dir
        report.error(t("bootstrap.doctor.manifest_dir", manifest=MANIFEST, recorded=recorded, skill_dir=skill_dir))
    elif skill_dir != "." and manifest.version != skill_version():
        report.todo(t("bootstrap.doctor.manifest_version", recorded=manifest.version, version=skill_version()))
    else:
        count = len(manifest.files)
        report.ok(t("bootstrap.doctor.manifest_ok", manifest=MANIFEST, version=manifest.version, count=count))

    by_language = planned_by_language(root, skill_dir, cfg.default_branch, cfg, git, selftest=False)
    for name, content in by_language[cfg.language].items():
        target = root / name
        optional = name.endswith("semver-analysis.yml")
        current = _read(target)
        if current is None:
            if optional:
                report.ok(t("bootstrap.doctor.absent_optional", name=name))
            else:
                report.todo(t("bootstrap.doctor.absent", name=name))
            continue
        problems = workflow_problems(name, current, cfg, skill_dir) if ".github/workflows/" in name else []
        for problem in problems:
            report.error(t("bootstrap.doctor.mismatch", name=name, problem=problem))
        # Um modelo não editado, mas de outro idioma: a chave `language` mudou e falta o `update`.
        other = None
        if current != content:
            others = (lang for lang in by_language if lang != cfg.language)
            other = next((lang for lang in others if by_language[lang].get(name) == current), None)
        if other is not None:
            report.todo(t("bootstrap.doctor.other_language", name=name, found=other, language=cfg.language))
        elif ".github/workflows/" not in name or current == content:
            report.ok(name)
        elif manifest is not None and manifest.pristine(name, current):
            report.todo(t("bootstrap.doctor.stale", name=name))
        elif not problems:
            report.todo(t("bootstrap.doctor.differs", name=name))
    language_report(report, root, cfg, manifest)
    template = root / ".github/pull_request_template.md"
    try:
        template_text = template.read_text(encoding="utf-8") if template.is_file() else None
    except UnicodeDecodeError:
        report.error(t("bootstrap.doctor.template_encoding", path=template.relative_to(root).as_posix()))
        template_text = None
    if template_text is not None:
        present = sections(template_text)
        missing = [name for name in cfg.pr["required_sections"] if find_section(present, name) is None]
        if missing:
            report.error(t("bootstrap.doctor.template_sections", missing=", ".join(missing)))

    hook_report(report, hook_path(git), skill_dir)
    claude_hooks_report(report, root, skill_dir)

    repo = Repo(cfg, git)
    if cfg.versioning_enabled:
        latest = repo.current("HEAD") if git.ok("rev-parse", "HEAD") else None
        files_version = latest[1] if latest else Version.parse(str(cfg.versioning["initial_version"]))
        problems = check_version_files(cfg, files_version)
        for problem in problems:
            report.error(problem)
        if not problems:
            report.ok(t("bootstrap.doctor.version_files", count=len(cfg.versioning["files"]), version=files_version))
        if latest is None:
            report.todo(t("bootstrap.doctor.no_tag", tag=f"{cfg.tag_prefix}{current}"))
        else:
            report.ok(t("bootstrap.doctor.tag", tag=latest[0]))

    repository = current_repository(git)
    github = GitHub(repository)
    if not github.enabled:
        report.todo(t("bootstrap.doctor.offline"))
    else:
        completed = run_gh(["label", "list", "--repo", str(repository), "--limit", "300", "--json", "name"])
        if completed.returncode != 0:
            error = completed.stderr.strip() or t("bootstrap.doctor.gh_unavailable")
            report.todo(t("bootstrap.doctor.labels_error", error=error))
        else:
            existing = {item["name"] for item in json.loads(completed.stdout or "[]")}
            missing = [name for name in cfg.all_labels() if name not in existing]
            if missing:
                report.todo(t("bootstrap.doctor.labels_missing", labels=", ".join(missing)))
            else:
                report.ok(t("bootstrap.doctor.labels_ok", count=len(cfg.all_labels())))
        permissions = github.api("actions/permissions/workflow") if cfg.release["mode"] == "pull-request" else None
        if permissions and not permissions.get("can_approve_pull_request_reviews"):
            report.todo(t("bootstrap.doctor.pr_permission"))
        protection_report(report, github, cfg.default_branch, required_check(cfg.language))
    print(t("bootstrap.doctor.owner_steps", check=required_check(cfg.language), skill_dir=skill_dir))
    print(t("bootstrap.doctor.summary", errors=report.errors, pending=report.pending))
    return 1 if report.errors else 0


def main(argv: Sequence[str] | None = None) -> int:
    """Códigos de saída: 0 ok, 1 falha (``doctor`` com erro ou erro de operação), 2 uso."""
    utf8_streams()
    try:
        return _main(argv)
    except (CommandError, ConfigError, ReleaseError) as exc:
        print(t("bootstrap.error", message=exc), file=sys.stderr)
        return 1
    except UnicodeDecodeError as exc:
        print(t("bootstrap.error", message=t("bootstrap.encoding", reason=exc.reason)), file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        print(t("bootstrap.error", message=exc), file=sys.stderr)
        return 1


def _activate_project_language() -> None:
    """Mensagens (e a ajuda) no idioma do projeto desde o começo; configuração inválida fica para o comando."""
    try:
        load(find_root())
    except (ConfigError, OSError, ValueError):
        i18n.activate(i18n.DEFAULT_LANGUAGE)


def _main(argv: Sequence[str] | None = None) -> int:
    _activate_project_language()
    parser = argparse.ArgumentParser(prog="bootstrap.py", description=t("bootstrap.help.description"))
    sub = parser.add_subparsers(dest="command", required=True)
    command = sub.add_parser("init")
    command.add_argument("--skill-dir", default="", help=t("bootstrap.help.skill_dir", default=VENDOR_PATH))
    command.add_argument(
        "--language", default="", help=t("bootstrap.help.language", accepted=", ".join(i18n.LANGUAGES))
    )
    command.add_argument("--default-branch", default="")
    command.add_argument("--initial-version", default="")
    command.add_argument("--force", action="store_true", help=t("bootstrap.help.force_init"))
    command.add_argument("--no-labels", action="store_true")
    command.add_argument("--selftest", action="store_true", help=t("bootstrap.help.selftest"))
    command.add_argument("--dry-run", action="store_true")
    command = sub.add_parser("update", help=t("bootstrap.help.update"))
    command.add_argument("--force", action="store_true", help=t("bootstrap.help.force_update"))
    command.add_argument("--dry-run", action="store_true")
    command = sub.add_parser("uninstall", help=t("bootstrap.help.uninstall"))
    command.add_argument("--yes", action="store_true", help=t("bootstrap.help.yes"))
    command = sub.add_parser("install-hook")
    command.add_argument("--force", action="store_true")
    sub.add_parser("doctor")
    sub.add_parser("version", help=t("bootstrap.help.version"))
    args = parser.parse_args(argv)
    if args.command == "init":
        return init(args)
    if args.command == "update":
        return update(args)
    if args.command == "uninstall":
        return uninstall(args)
    if args.command == "version":
        print(skill_version())
        return 0
    if args.command == "install-hook":
        root = find_root()
        skill_dir = skill_dir_in(root)
        if skill_dir is None:
            return _refuse(t("bootstrap.install_hook.outside"))
        print(install_hook(root, skill_dir, force=args.force))
        return 0
    return doctor(args)
