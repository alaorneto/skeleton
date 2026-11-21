"""Idioma do fluxo: catálogo de mensagens e padrões de configuração que dependem dele.

Cada idioma é um módulo de ``locales/`` com dois dicionários: ``MESSAGES`` (os textos que as
ferramentas escrevem) e ``DEFAULTS`` (os padrões de configuração que mudam com o idioma: nomes
de seção, marcadores, títulos do changelog, descrições dos tipos). São dicionários Python de
propósito: gettext exigiria arquivos ``.mo`` compilados e TOML não quebra linhas longas.

Uma chave que falta no idioma cai para o pt-BR e, se também faltar lá, para a própria chave:
uma tradução incompleta nunca derruba um comando.
"""

from __future__ import annotations

import copy
import functools
import importlib
import string
from types import ModuleType
from typing import Any

DEFAULT_LANGUAGE = "pt-BR"
LANGUAGES = {"pt-BR": "pt_br", "en": "en"}  # idioma → módulo de locales/
_active = DEFAULT_LANGUAGE


def normalize(value: Any) -> str | None:
    """``pt-br``, ``pt_BR`` e ``PT-BR`` são ``pt-BR``; ``None`` para o que não é um idioma aceito."""
    if not isinstance(value, str):
        return None
    wanted = value.strip().replace("_", "-").casefold()
    return next((name for name in LANGUAGES if name.casefold() == wanted), None)


@functools.cache
def _locale(language: str) -> ModuleType:
    return importlib.import_module(f".locales.{LANGUAGES[language]}", __package__)


def messages(language: str) -> dict[str, str]:
    return _locale(language).MESSAGES


def defaults(language: str) -> dict[str, Any]:
    """Cópia dos padrões de configuração do idioma."""
    return copy.deepcopy(_locale(language).DEFAULTS)


# Nomes de seção dos padrões: o nome na mesma posição em cada idioma é o mesmo nome em outra língua.
_SECTION_DEFAULTS = (
    ("issues", "items_section"),
    ("issues", "depends_marker"),
    ("issues", "acceptance_section"),
    ("issues", "sections", "default"),
    ("issues", "sections", "bug"),
    ("issues", "sections", "documentation"),
    ("pr", "required_sections"),
    ("pr", "version_section"),
    ("pr", "tests_section"),
    ("pr", "docs_section"),
    # Não é seção, mas é lido do corpo do PR do mesmo jeito: `Motivo da sobreposição` e `Override reason`.
    ("pr", "override_reason_marker"),
)


@functools.cache
def section_groups() -> tuple[frozenset[str], ...]:
    """Grupos de nomes de seção equivalentes entre os idiomas (``{"Versão", "Version"}``).

    A tabela sai dos padrões de todos os idiomas, posição a posição: um idioma novo entra nela sem
    mudar nada aqui. Um PR ou uma issue aberta antes de o projeto trocar de idioma continua valendo.
    """
    groups: dict[str, frozenset[str]] = {}
    for path in _SECTION_DEFAULTS:
        columns = []
        for language in LANGUAGES:
            value: Any = defaults(language)
            for key in path:
                value = value[key]
            columns.append(value if isinstance(value, list) else [value])
        for names in zip(*columns, strict=True):
            merged = frozenset(names).union(*(groups.get(name, frozenset()) for name in names))
            for name in merged:
                groups[name] = merged
    return tuple(sorted(set(groups.values()), key=sorted))


class _Keep(dict):
    """Marcador sem valor continua no texto, em vez de derrubar a formatação."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def translate(language: str, key: str, /, **values: Any) -> str:
    """O texto de ``key`` em ``language``, com os marcadores ``{nome}`` preenchidos."""
    template = None
    for candidate in (language, DEFAULT_LANGUAGE):
        if candidate in LANGUAGES:
            template = messages(candidate).get(key)
        if template is not None:
            break
    if template is None:
        return key
    try:
        return string.Formatter().vformat(template, (), _Keep(values))
    except (ValueError, IndexError, KeyError, AttributeError):
        return template


def activate(language: str) -> None:
    """Idioma das mensagens de quem não tem uma ``Config`` à mão. ``config.load`` chama isto."""
    global _active  # noqa: PLW0603
    _active = language if language in LANGUAGES else DEFAULT_LANGUAGE


def active() -> str:
    return _active


def t(key: str, /, **values: Any) -> str:
    """``translate`` no idioma ativo."""
    return translate(_active, key, **values)
