"""SemVer 2.0.0: leitura, precedência e incremento. Só biblioteca padrão."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

LEVELS = ("none", "patch", "minor", "major")
ZERO_MAJOR_POLICIES = ("breaking-is-minor", "strict")

_IDENT = r"(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)"
_SEMVER = re.compile(
    r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    rf"(?:-(?P<pre>{_IDENT}(?:\.{_IDENT})*))?"
    r"(?:\+(?P<build>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
_PEP440_CHANNELS = {"alpha": "a", "a": "a", "beta": "b", "b": "b", "rc": "rc", "c": "rc", "pre": "rc", "preview": "rc"}


@dataclass(frozen=True)
class Version:
    major: int
    minor: int
    patch: int
    pre: tuple[str, ...] = ()
    build: str = ""

    @classmethod
    def parse(cls, text: str) -> Version:
        match = _SEMVER.match(text.strip()) if isinstance(text, str) else None
        if not match:
            raise ValueError(f"'{text}' não é uma versão SemVer (major.minor.patch[-pre][+build]).")
        pre = tuple(match.group("pre").split(".")) if match.group("pre") else ()
        return cls(
            int(match.group("major")), int(match.group("minor")), int(match.group("patch")), pre, match["build"] or ""
        )

    @classmethod
    def try_parse(cls, text: str) -> Version | None:
        try:
            return cls.parse(text)
        except ValueError:
            return None

    def __str__(self) -> str:
        text = self.core
        if self.pre:
            text += "-" + ".".join(self.pre)
        if self.build:
            text += "+" + self.build
        return text

    @property
    def core(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"

    @property
    def is_prerelease(self) -> bool:
        return bool(self.pre)

    @property
    def final(self) -> Version:
        return Version(self.major, self.minor, self.patch)

    def key(self) -> tuple:
        """Chave de precedência (item 11 da especificação). O build não participa."""
        identifiers = tuple((0, int(item), "") if item.isdigit() else (1, 0, item) for item in self.pre)
        return (self.major, self.minor, self.patch, 0 if self.pre else 1, identifiers)

    def pep440(self) -> str:
        """Forma PEP 440 para os canais conhecidos (``1.2.0-rc.1`` vira ``1.2.0rc1``)."""
        if not self.pre:
            return self.core
        channel = _PEP440_CHANNELS.get(self.pre[0].lower())
        if channel is None or len(self.pre) > 2 or (len(self.pre) == 2 and not self.pre[1].isdigit()):
            return str(self)
        return f"{self.core}{channel}{self.pre[1] if len(self.pre) == 2 else 0}"

    def bump(self, level: str) -> Version:
        if level == "major":
            return Version(self.major + 1, 0, 0)
        if level == "minor":
            return Version(self.major, self.minor + 1, 0)
        if level == "patch":
            return Version(self.major, self.minor, self.patch + 1)
        raise ValueError(f"Nível '{level}' não incrementa a versão.")


def level_index(level: str) -> int:
    if level not in LEVELS:
        raise ValueError(f"Nível desconhecido: '{level}'. Use um de {', '.join(LEVELS)}.")
    return LEVELS.index(level)


def max_level(levels: Iterable[str]) -> str:
    return LEVELS[max((level_index(level) for level in levels), default=0)]


def effective_level(current: Version, level: str, zero_major: str = "breaking-is-minor") -> str:
    """Nível que de fato incrementa a versão.

    Em ``0.y.z`` a especificação (item 4) diz que a API pública não é estável. A convenção adotada
    é a do npm e do Cargo: quebra de compatibilidade sobe o minor. Chegar a ``1.0.0`` é uma decisão
    de quem mantém o projeto, tomada por sobreposição explícita, nunca pelo cálculo.
    """
    if zero_major not in ZERO_MAJOR_POLICIES:
        raise ValueError(f"Política de 0.x desconhecida: '{zero_major}'.")
    if current.major == 0 and level == "major" and zero_major == "breaking-is-minor":
        return "minor"
    return level


def next_version(current: Version, level: str, zero_major: str = "breaking-is-minor") -> Version:
    return current.final.bump(effective_level(current, level, zero_major))


def next_prerelease(target: Version, channel: str, existing: Iterable[Version]) -> Version:
    """Próxima pré-release de ``target`` no canal: ``1.2.0-rc.1``, depois ``-rc.2``."""
    if not re.fullmatch(r"[A-Za-z][0-9A-Za-z-]*", channel):
        raise ValueError(f"Canal de pré-release inválido: '{channel}'.")
    numbers = [
        int(version.pre[1])
        for version in existing
        if version.final == target.final and len(version.pre) == 2 and version.pre[0] == channel
        if version.pre[1].isdigit()
    ]
    return Version(target.major, target.minor, target.patch, (channel, str(max(numbers, default=0) + 1)))


def highest(versions: Iterable[Version]) -> Version | None:
    ordered = sorted(versions, key=Version.key)
    return ordered[-1] if ordered else None
