"""Project configuration of the dev-workflow skill (``.github/dev-workflow.toml``).

Only the standard library (Python 3.11+ for ``tomllib``). Everything project specific lives in the
file: type labels and their commit types, required sections of issues and pull requests, the
default branch, pull request rules and versioning. ``load`` validates every key and reports the
first invalid one by its dotted path, so a typo never becomes a silent default.
"""

from __future__ import annotations

import re
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CONFIG_PATH = Path(".github") / "dev-workflow.toml"
_COLOR = re.compile(r"[0-9a-fA-F]{6}\Z")
_LABEL = re.compile(r"[a-z0-9][a-z0-9:._-]{0,49}\Z")
_COMMIT_TYPE = re.compile(r"[a-z]{2,16}\Z")
_LEVELS = ("patch", "minor", "major")


class ConfigError(ValueError):
    """An invalid configuration; the message starts with the dotted key."""


@dataclass(frozen=True)
class Label:
    name: str
    color: str
    description: str


@dataclass(frozen=True)
class TypeLabel(Label):
    commit_types: tuple[str, ...] = ()
    level: str = "patch"  # The SemVer level a released change of this type implies.


@dataclass(frozen=True)
class VersionFile:
    """One projection of the version: ``search`` (literal, ``{version}`` placeholder) or ``regex``."""

    path: str
    replace: str
    search: str | None = None
    regex: str | None = None


@dataclass(frozen=True)
class Versioning:
    enabled: bool = False
    tag_prefix: str = "v"
    changelog: str = "CHANGELOG.md"
    release_label: str = "release"
    breaking_label: str = "breaking-change"
    override_prefix: str = "semver:"
    files: tuple[VersionFile, ...] = ()
    public_surfaces: tuple[str, ...] = ()  # Globs whose change raises the level to minor at least.
    floors: tuple[VersionFile, ...] = ()  # Version floors in code: never above the published version.
    mode: str = "direct"  # "direct" commits and tags on the default branch; "pr" opens a release PR.
    analysis_authors: tuple[str, ...] = ("github-actions[bot]",)


@dataclass(frozen=True)
class Config:
    default_branch: str = "main"
    language: str = "pt"
    types: tuple[TypeLabel, ...] = ()
    extra_labels: tuple[Label, ...] = ()
    epic_label: str = "epic"
    items_section: str = "Itens"
    depends_marker: str = "depende de"
    issue_sections: tuple[str, ...] = ()
    pr_sections: tuple[str, ...] = ()
    pr_exempt_authors: tuple[str, ...] = ("dependabot[bot]",)
    pr_title_conventional: bool = True
    pr_pending_criteria: str = "warning"  # "warning" or "error"
    versioning: Versioning = field(default_factory=Versioning)
    root: Path = Path(".")

    @property
    def type_names(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.types)

    def type_for_commit(self, commit_type: str) -> TypeLabel | None:
        return next((item for item in self.types if commit_type in item.commit_types), None)

    def label(self, name: str) -> Label | None:
        return next((item for item in (*self.types, *self.extra_labels) if item.name == name), None)


def _expect(value: Any, kind: type | tuple[type, ...], key: str) -> Any:
    if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
        names = kind.__name__ if isinstance(kind, type) else " ou ".join(item.__name__ for item in kind)
        raise ConfigError(f"{key}: esperado {names}, encontrado {type(value).__name__}.")
    return value


def _strings(value: Any, key: str) -> tuple[str, ...]:
    _expect(value, list, key)
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise ConfigError(f"{key}[{index}]: esperado texto não vazio.")
    return tuple(value)


def _closed(table: dict[str, Any], allowed: set[str], key: str) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise ConfigError(f"{key}.{unknown[0]}: chave desconhecida.")


def _label(name: str, table: Any, key: str, *, typed: bool) -> Label:
    _expect(table, dict, key)
    allowed = {"color", "description"} | ({"commit", "level"} if typed else set())
    _closed(table, allowed, key)
    if not _LABEL.fullmatch(name):
        raise ConfigError(f"{key}: nome de label inválido.")
    color = _expect(table.get("color"), str, f"{key}.color")
    if not _COLOR.fullmatch(color):
        raise ConfigError(f"{key}.color: esperado hexadecimal de seis dígitos.")
    description = _expect(table.get("description"), str, f"{key}.description")
    if not typed:
        return Label(name, color.lower(), description)
    commits = _strings(table.get("commit", []), f"{key}.commit")
    for index, item in enumerate(commits):
        if not _COMMIT_TYPE.fullmatch(item):
            raise ConfigError(f"{key}.commit[{index}]: tipo de commit inválido.")
    level = table.get("level", "patch")
    if level not in _LEVELS:
        raise ConfigError(f"{key}.level: use patch, minor ou major.")
    return TypeLabel(name, color.lower(), description, commits, level)


def _version_file(table: Any, key: str) -> VersionFile:
    _expect(table, dict, key)
    _closed(table, {"path", "search", "regex", "replace"}, key)
    path = _expect(table.get("path"), str, f"{key}.path")
    if Path(path).is_absolute() or ".." in Path(path).parts:
        raise ConfigError(f"{key}.path: caminho relativo ao repositório, sem '..'.")
    replace = _expect(table.get("replace"), str, f"{key}.replace")
    search, regex = table.get("search"), table.get("regex")
    if (search is None) == (regex is None):
        raise ConfigError(f"{key}: informe exatamente um de search ou regex.")
    if search is not None and "{version}" not in _expect(search, str, f"{key}.search"):
        raise ConfigError(f"{key}.search: precisa conter {{version}}.")
    if regex is not None:
        try:
            compiled = re.compile(_expect(regex, str, f"{key}.regex"), re.MULTILINE)
        except re.error as exc:
            raise ConfigError(f"{key}.regex: expressão inválida ({exc}).") from None
        if "version" not in compiled.groupindex:
            raise ConfigError(f"{key}.regex: precisa do grupo nomeado (?P<version>...).")
    if "{version}" not in replace:
        raise ConfigError(f"{key}.replace: precisa conter {{version}}.")
    return VersionFile(path, replace, search, regex)


def _versioning(table: Any) -> Versioning:
    key = "versioning"
    _expect(table, dict, key)
    _closed(
        table,
        {
            "enabled",
            "tag_prefix",
            "changelog",
            "release_label",
            "breaking_label",
            "override_prefix",
            "files",
            "public_surfaces",
            "floors",
            "mode",
            "analysis_authors",
        },
        key,
    )
    default = Versioning()
    mode = table.get("mode", default.mode)
    if mode not in ("direct", "pr"):
        raise ConfigError(f"{key}.mode: use direct ou pr.")
    files = tuple(
        _version_file(item, f"{key}.files[{index}]")
        for index, item in enumerate(_expect(table.get("files", []), list, f"{key}.files"))
    )
    floors = tuple(
        _version_file(item, f"{key}.floors[{index}]")
        for index, item in enumerate(_expect(table.get("floors", []), list, f"{key}.floors"))
    )
    enabled = _expect(table.get("enabled", default.enabled), bool, f"{key}.enabled")
    if enabled and not files:
        raise ConfigError(f"{key}.files: o versionamento ativo exige ao menos um arquivo.")
    return Versioning(
        enabled=enabled,
        tag_prefix=_expect(table.get("tag_prefix", default.tag_prefix), str, f"{key}.tag_prefix"),
        changelog=_expect(table.get("changelog", default.changelog), str, f"{key}.changelog"),
        release_label=_expect(table.get("release_label", default.release_label), str, f"{key}.release_label"),
        breaking_label=_expect(table.get("breaking_label", default.breaking_label), str, f"{key}.breaking_label"),
        override_prefix=_expect(table.get("override_prefix", default.override_prefix), str, f"{key}.override_prefix"),
        files=files,
        public_surfaces=_strings(table.get("public_surfaces", []), f"{key}.public_surfaces"),
        floors=floors,
        mode=mode,
        analysis_authors=_strings(
            table.get("analysis_authors", list(default.analysis_authors)), f"{key}.analysis_authors"
        ),
    )


def parse(data: dict[str, Any], *, root: Path = Path(".")) -> Config:
    _closed(data, {"project", "types", "labels", "issue", "pull_request", "versioning"}, "")
    project = _expect(data.get("project", {}), dict, "project")
    _closed(project, {"default_branch", "language"}, "project")
    types_table = _expect(data.get("types"), dict, "types")
    if not types_table:
        raise ConfigError("types: declare ao menos um label de tipo.")
    types = tuple(_label(name, value, f"types.{name}", typed=True) for name, value in types_table.items())
    seen: dict[str, str] = {}
    for item in types:
        for commit in item.commit_types:
            if commit in seen:
                raise ConfigError(f"types.{item.name}.commit: '{commit}' já pertence a types.{seen[commit]}.")
            seen[commit] = item.name
    labels = _expect(data.get("labels", {}), dict, "labels")
    extra = tuple(_label(name, value, f"labels.{name}", typed=False) for name, value in labels.items())
    duplicated = sorted({item.name for item in types} & {item.name for item in extra})
    if duplicated:
        raise ConfigError(f"labels.{duplicated[0]}: já declarado em types.")
    issue = _expect(data.get("issue", {}), dict, "issue")
    _closed(issue, {"sections", "epic_label", "items_section", "depends_marker"}, "issue")
    pr = _expect(data.get("pull_request", {}), dict, "pull_request")
    _closed(pr, {"sections", "exempt_authors", "conventional_title", "pending_criteria"}, "pull_request")
    pending = pr.get("pending_criteria", "warning")
    if pending not in ("warning", "error"):
        raise ConfigError("pull_request.pending_criteria: use warning ou error.")
    default = Config()
    return Config(
        default_branch=_expect(project.get("default_branch", default.default_branch), str, "project.default_branch"),
        language=_expect(project.get("language", default.language), str, "project.language"),
        types=types,
        extra_labels=extra,
        epic_label=_expect(issue.get("epic_label", default.epic_label), str, "issue.epic_label"),
        items_section=_expect(issue.get("items_section", default.items_section), str, "issue.items_section"),
        depends_marker=_expect(issue.get("depends_marker", default.depends_marker), str, "issue.depends_marker"),
        issue_sections=_strings(issue.get("sections", []), "issue.sections"),
        pr_sections=_strings(pr.get("sections", []), "pull_request.sections"),
        pr_exempt_authors=_strings(
            pr.get("exempt_authors", list(default.pr_exempt_authors)), "pull_request.exempt_authors"
        ),
        pr_title_conventional=_expect(pr.get("conventional_title", True), bool, "pull_request.conventional_title"),
        pr_pending_criteria=pending,
        versioning=_versioning(data["versioning"]) if "versioning" in data else Versioning(),
        root=root,
    )


def repository_root(start: Path | None = None) -> Path:
    try:
        top = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], cwd=start, capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return Path(start or ".").resolve()
    return Path(top)


def load(root: Path | None = None) -> Config:
    """The configuration of the repository at root (default: the current one)."""
    root = repository_root(root)
    path = root / CONFIG_PATH
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"{CONFIG_PATH.as_posix()}: arquivo não encontrado; rode bootstrap.py init.") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{CONFIG_PATH.as_posix()}: TOML inválido ({exc}).") from None
    return parse(data, root=root)


# The template the skill ships (assets/dev-workflow.toml): the defaults of every rule.
TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "assets" / "dev-workflow.toml"
DEFAULT = parse(tomllib.loads(TEMPLATE_PATH.read_text(encoding="utf-8")))
