"""Configuração do fluxo por projeto (``.github/dev-workflow.toml``).

Tudo que é específico de um projeto (tipos de issue, seções, arquivos de versão, superfícies
públicas) vive nesse arquivo. Sem arquivo, valem os padrões abaixo, que bastam para o hook
``commit-msg``. As tabelas do arquivo se sobrepõem aos padrões chave a chave; ``types`` e as
listas substituem o padrão por inteiro, para que um projeto possa ter menos tipos ou seções.
"""

from __future__ import annotations

import copy
import fnmatch
import os
import re
import tomllib
from pathlib import Path
from typing import Any

from .semver import LEVELS, ZERO_MAJOR_POLICIES, Version

CONFIG_ENV = "DEV_WORKFLOW_CONFIG"
CONFIG_CANDIDATES = (".github/dev-workflow.toml", "dev-workflow.toml", ".dev-workflow.toml")
SURFACE_ACTIONS = (*LEVELS, "review")
SEVERITIES = ("error", "warning", "off")
RELEASE_MODES = ("direct", "pull-request")

_COMMON_SECTIONS = [
    "Contexto",
    "Objetivo",
    "Escopo",
    "Abordagem técnica",
    "Critérios de aceite",
    "Plano de testes",
    "Documentação a atualizar",
    "Riscos e dependências",
    "Definição de pronto",
]

DEFAULTS: dict[str, Any] = {
    "schema": 1,
    "repository": {"default_branch": "main", "release_branches": ["main", "release/*"]},
    "types": {
        "bug": {
            "color": "d73a4a",
            "description": "Comportamento incorreto ou regressão",
            "prefixes": ["fix"],
            "level": "patch",
            "section": "fixes",
        },
        "feature": {
            "color": "1d76db",
            "description": "Nova funcionalidade",
            "prefixes": ["feat"],
            "level": "minor",
            "section": "features",
        },
        "enhancement": {
            "color": "a2eeef",
            "description": "Melhoria de funcionalidade existente",
            "prefixes": ["feat", "perf"],
            "level": "minor",
            "section": "improvements",
        },
        "documentation": {
            "color": "0075ca",
            "description": "Somente documentação",
            "prefixes": ["docs"],
            "level": "none",
            "section": "docs",
        },
        "refactor": {
            "color": "fbca04",
            "description": "Reestruturação sem mudança de comportamento",
            "prefixes": ["refactor"],
            "level": "none",
            "section": "maintenance",
        },
        "test": {
            "color": "bfd4f2",
            "description": "Somente testes, fixtures ou evidência de verificação",
            "prefixes": ["test"],
            "level": "none",
            "section": "maintenance",
        },
        "ci": {
            "color": "5319e7",
            "description": "Pipeline e checks automáticos",
            "prefixes": ["ci"],
            "level": "none",
            "section": "maintenance",
        },
        "chore": {
            "color": "c5def5",
            "description": "Dependências, ferramentas e configuração de desenvolvimento",
            "prefixes": ["chore", "build"],
            "level": "none",
            "section": "maintenance",
        },
    },
    "labels": {
        "epic": "epic",
        "release": "release",
        "no_release": "no-release",
        "breaking": "breaking-change",
        "override_prefix": "semver:",
        "extra": {},
    },
    "commits": {"levels": {"feat": "minor", "fix": "patch", "perf": "patch"}},
    "issues": {
        "items_section": "Itens",
        "depends_marker": "depende de",
        "acceptance_section": "Critérios de aceite",
        "sections": {
            "default": _COMMON_SECTIONS,
            "bug": [
                "Comportamento observado",
                "Comportamento esperado",
                "Passos para reproduzir",
                *_COMMON_SECTIONS[2:],
            ],
            "documentation": ["Contexto", "Objetivo", "Critérios de aceite", "Definição de pronto"],
        },
    },
    "pr": {
        "required_sections": ["Issues", "Resumo", "Testes executados", "Documentação", "Versão"],
        "version_section": "Versão",
        "tests_section": "Testes executados",
        "docs_section": "Documentação",
        "override_reason_marker": "Motivo da sobreposição",
        "title_pattern": r"^(feat|fix|perf|docs|refactor|test|ci|chore|build|revert)(\([\w./ -]+\))?!?: \S.*",
        "exempt_authors": ["dependabot[bot]"],
        "severity": {
            "sections": "error",
            "classification": "error",
            "title": "error",
            "unchecked_criteria": "warning",
            "issue_sections": "warning",
            "type_mismatch": "warning",
        },
    },
    "versioning": {
        "enabled": True,
        "tag_prefix": "v",
        "initial_version": "0.1.0",
        "zero_major": "breaking-is-minor",
        "require_analysis": False,
        "trusted_bots": ["github-actions[bot]", "claude[bot]"],
        "files": [],
        "public_surfaces": [],
        "floors": [],
    },
    "changelog": {
        "path": "CHANGELOG.md",
        "title": "# Changelog",
        "intro": (
            "Todas as mudanças publicadas deste projeto. Este arquivo é escrito pela automação de release; "
            "não edite as seções de versão à mão."
        ),
        "prereleases": False,
        "hide": [],
        "sections": [
            {"id": "breaking", "title": "⚠ Mudanças incompatíveis"},
            {"id": "features", "title": "Funcionalidades"},
            {"id": "improvements", "title": "Melhorias"},
            {"id": "fixes", "title": "Correções"},
            {"id": "docs", "title": "Documentação"},
            {"id": "maintenance", "title": "Manutenção"},
        ],
    },
    "release": {
        "mode": "direct",
        "commit_message": "chore(release): {tag}",
        "pr_branch": "release/next",
        "mark": ["comment", "milestone"],
        "dispatch_workflow": "",
    },
}

BREAKING_SECTION = "breaking"
FALLBACK_SECTION = "maintenance"


class ConfigError(ValueError):
    """Configuração inválida; a mensagem aponta a chave."""


def _merge(base: dict[str, Any], override: dict[str, Any], path: str = "") -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in override.items():
        where = f"{path}.{key}" if path else key
        replace_whole = where in ("types", "labels.extra", "issues.sections", "commits.levels")
        if isinstance(value, dict) and isinstance(result.get(key), dict) and not replace_whole:
            result[key] = _merge(result[key], value, where)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _fail(key: str, message: str) -> None:
    raise ConfigError(f"dev-workflow.toml: `{key}` {message}")


class Config:
    """Acesso tipado ao dicionário validado."""

    def __init__(self, data: dict[str, Any] | None = None, *, path: Path | None = None, root: Path | None = None):
        self.data = _merge(DEFAULTS, data or {})
        self.path = path
        self.root = root or Path.cwd()
        self._validate()

    # --- validação -------------------------------------------------------------------------

    def _validate(self) -> None:
        d = self.data
        if d.get("schema") != 1:
            _fail("schema", "deve ser 1.")
        if not isinstance(d["types"], dict) or not d["types"]:
            _fail("types", "precisa de ao menos um tipo.")
        section_ids = {section.get("id") for section in d["changelog"]["sections"]}
        for needed in (BREAKING_SECTION, FALLBACK_SECTION):
            if needed not in section_ids:
                _fail("changelog.sections", f"precisa de uma seção com id `{needed}`.")
        for name, spec in d["types"].items():
            if spec.get("level", "none") not in LEVELS:
                _fail(f"types.{name}.level", f"deve ser um de {', '.join(LEVELS)}.")
            if not spec.get("prefixes"):
                _fail(f"types.{name}.prefixes", "precisa de ao menos um prefixo de commit e branch.")
            if spec.get("section", FALLBACK_SECTION) not in section_ids:
                _fail(f"types.{name}.section", "não corresponde a um id de `changelog.sections`.")
        for kind, level in d["commits"]["levels"].items():
            if level not in LEVELS:
                _fail(f"commits.levels.{kind}", f"deve ser um de {', '.join(LEVELS)}.")
        for key, value in d["pr"]["severity"].items():
            if value not in SEVERITIES:
                _fail(f"pr.severity.{key}", f"deve ser um de {', '.join(SEVERITIES)}.")
        try:
            re.compile(d["pr"]["title_pattern"])
        except re.error as exc:
            _fail("pr.title_pattern", f"não é uma expressão regular válida: {exc}.")
        v = d["versioning"]
        if v["zero_major"] not in ZERO_MAJOR_POLICIES:
            _fail("versioning.zero_major", f"deve ser um de {', '.join(ZERO_MAJOR_POLICIES)}.")
        if Version.try_parse(str(v["initial_version"])) is None:
            _fail("versioning.initial_version", "não é uma versão SemVer.")
        for index, entry in enumerate(v["files"]):
            key = f"versioning.files[{index}]"
            if not entry.get("path"):
                _fail(f"{key}.path", "é obrigatório.")
            if bool(entry.get("search")) == bool(entry.get("regex")):
                _fail(key, "precisa de exatamente um entre `search` e `regex`.")
            if entry.get("regex") and "replace" not in entry:
                _fail(f"{key}.replace", "é obrigatório com `regex`.")
            if not isinstance(entry.get("count", 1), int) or entry.get("count", 1) < 1:
                _fail(f"{key}.count", "deve ser um inteiro maior que zero.")
        for index, entry in enumerate(v["public_surfaces"]):
            key = f"versioning.public_surfaces[{index}]"
            if not entry.get("globs"):
                _fail(f"{key}.globs", "precisa de ao menos um padrão.")
            for event in ("on_add", "on_modify", "on_remove"):
                if entry.get(event, "review") not in SURFACE_ACTIONS:
                    _fail(f"{key}.{event}", f"deve ser um de {', '.join(SURFACE_ACTIONS)}.")
        for index, entry in enumerate(v["floors"]):
            key = f"versioning.floors[{index}]"
            if not entry.get("globs"):
                _fail(f"{key}.globs", "precisa de ao menos um padrão.")
            try:
                if re.compile(entry.get("regex", "")).groups < 1:
                    _fail(f"{key}.regex", "precisa de um grupo de captura com a versão.")
            except re.error as exc:
                _fail(f"{key}.regex", f"não é uma expressão regular válida: {exc}.")
        r = d["release"]
        if r["mode"] not in RELEASE_MODES:
            _fail("release.mode", f"deve ser um de {', '.join(RELEASE_MODES)}.")
        if "{tag}" not in r["commit_message"]:
            _fail("release.commit_message", "precisa conter `{tag}`.")
        for item in r["mark"]:
            if item not in ("comment", "milestone"):
                _fail("release.mark", "aceita `comment` e `milestone`.")

    # --- tipos e labels --------------------------------------------------------------------

    @property
    def types(self) -> dict[str, dict[str, Any]]:
        return self.data["types"]

    @property
    def type_labels(self) -> list[str]:
        return list(self.types)

    def label(self, name: str) -> str:
        return str(self.data["labels"][name])

    def type_level(self, type_label: str) -> str:
        return str(self.types[type_label].get("level", "none"))

    def type_section(self, type_label: str) -> str:
        return str(self.types[type_label].get("section", FALLBACK_SECTION))

    def type_for_prefix(self, prefix: str) -> str | None:
        """Primeiro tipo que aceita o prefixo de commit (``fix`` → ``bug``)."""
        return next((name for name, spec in self.types.items() if prefix in spec["prefixes"]), None)

    def all_labels(self) -> dict[str, tuple[str, str]]:
        """Labels que o fluxo usa: ``{nome: (cor, descrição)}``."""
        labels = {name: (spec.get("color", "ededed"), spec.get("description", "")) for name, spec in self.types.items()}
        labels[self.label("epic")] = ("3e4b9e", "Acompanhamento de um plano com várias issues")
        for name, spec in self.data["labels"]["extra"].items():
            labels[name] = (spec.get("color", "ededed"), spec.get("description", ""))
        if self.versioning_enabled:
            labels[self.label("release")] = (
                "0e8a16",
                "O merge deste PR publica uma versão com tudo que está acumulado",
            )
            labels[self.label("no_release")] = ("e4e669", "O PR não publica versão; fica acumulado para a próxima")
            labels[self.label("breaking")] = ("b60205", "Quebra de compatibilidade para quem usa o projeto")
            for level, text in (("major", "incompatível"), ("minor", "funcionalidade"), ("patch", "correção")):
                labels[self.override_label(level)] = ("d4c5f9", f"Sobrepõe o nível calculado: {level} ({text})")
        return labels

    def override_label(self, level: str) -> str:
        return f"{self.label('override_prefix')}{level}"

    # --- demais seções ---------------------------------------------------------------------

    @property
    def default_branch(self) -> str:
        return str(self.data["repository"]["default_branch"])

    def is_release_branch(self, branch: str) -> bool:
        return any(fnmatch.fnmatchcase(branch, pattern) for pattern in self.data["repository"]["release_branches"])

    @property
    def issues(self) -> dict[str, Any]:
        return self.data["issues"]

    def issue_sections(self, type_label: str | None) -> list[str]:
        sections = self.issues["sections"]
        return list(sections.get(type_label or "", sections.get("default", [])))

    @property
    def pr(self) -> dict[str, Any]:
        return self.data["pr"]

    def severity(self, rule: str) -> str:
        return str(self.pr["severity"].get(rule, "error"))

    @property
    def versioning(self) -> dict[str, Any]:
        return self.data["versioning"]

    @property
    def versioning_enabled(self) -> bool:
        return bool(self.versioning["enabled"])

    @property
    def tag_prefix(self) -> str:
        return str(self.versioning["tag_prefix"])

    @property
    def changelog(self) -> dict[str, Any]:
        return self.data["changelog"]

    @property
    def release(self) -> dict[str, Any]:
        return self.data["release"]

    def release_subject(self, tag: str) -> str:
        return str(self.release["commit_message"]).replace("{tag}", tag)

    def release_subject_pattern(self) -> re.Pattern[str]:
        """Reconhece o commit de release, inclusive com o sufixo ``(#N)`` de um squash."""
        template = re.escape(str(self.release["commit_message"])).replace(re.escape("{tag}"), r"(?P<tag>\S+)")
        return re.compile(rf"^{template}(?: \(#\d+\))?$")


def find_root(start: Path | None = None) -> Path:
    """Raiz do repositório git que contém ``start``; sem git, o próprio diretório."""
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return current


def find_config(root: Path) -> Path | None:
    explicit = os.environ.get(CONFIG_ENV)
    if explicit:
        return Path(explicit)
    return next((root / name for name in CONFIG_CANDIDATES if (root / name).is_file()), None)


def load(root: Path | None = None) -> Config:
    root = root or find_root()
    path = find_config(root)
    if path is None:
        return Config(root=root)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    return Config(data, path=path, root=root)
