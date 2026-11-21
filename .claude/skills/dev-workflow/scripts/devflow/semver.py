"""SemVer 2.0.0: leitura, precedência e incremento. Só biblioteca padrão."""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from . import i18n

LEVELS = ("none", "patch", "minor", "major")
ZERO_MAJOR_POLICIES = ("breaking-is-minor", "strict")

_IDENT = r"(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)"
_SEMVER = re.compile(
    r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    rf"(?:-(?P<pre>{_IDENT}(?:\.{_IDENT})*))?"
    r"(?:\+(?P<build>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
# Canais que têm forma PEP 440 e a forma de cada um (``1.2.0-beta.1`` vira ``1.2.0b1``).
PEP440_CHANNELS = {"alpha": "a", "a": "a", "beta": "b", "b": "b", "rc": "rc", "c": "rc", "pre": "rc", "preview": "rc"}
# Nome de canal aceito na configuração: minúsculas, para que a ordem ASCII seja a do SemVer.
CHANNEL_NAME = re.compile(r"[a-z][0-9a-z-]*")


def pep440_channel(channel: str) -> str | None:
    """A forma PEP 440 do canal (``a``, ``b`` ou ``rc``), ou ``None`` quando ele não tem."""
    return PEP440_CHANNELS.get(channel.lower())


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
            raise ValueError(i18n.t("semver.parse", text=text))
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
        channel = pep440_channel(self.pre[0])
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
        raise ValueError(i18n.t("semver.bump", level=level))


def level_index(level: str) -> int:
    if level not in LEVELS:
        raise ValueError(i18n.t("semver.level.unknown", level=level, accepted=", ".join(LEVELS)))
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
        raise ValueError(i18n.t("semver.zero_major.unknown", policy=zero_major))
    if current.major == 0 and level == "major" and zero_major == "breaking-is-minor":
        return "minor"
    return level


def next_version(current: Version, level: str, zero_major: str = "breaking-is-minor") -> Version:
    return current.final.bump(effective_level(current, level, zero_major))


def next_prerelease(
    target: Version, channel: str, existing: Iterable[Version], allowed: Sequence[str] | None = None
) -> Version:
    """Próxima pré-release de ``target`` no canal: ``1.2.0-rc.1``, depois ``-rc.2``.

    ``allowed`` é a lista de canais do projeto; um canal fora dela é recusado.
    """
    if not re.fullmatch(r"[A-Za-z][0-9A-Za-z-]*", channel):
        raise ValueError(i18n.t("semver.channel.invalid", channel=channel))
    if allowed is not None and channel not in allowed:
        accepted = ", ".join(allowed) or i18n.t("semver.channel.empty_list")
        raise ValueError(i18n.t("semver.channel.outside", channel=channel, accepted=accepted))
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


def cycle_floor(cycle: Iterable[Version]) -> Version | None:
    """O maior núcleo ``X.Y.Z`` que as pré-releases do ciclo já publicaram: o alvo não desce dele."""
    return highest(version.final for version in cycle)


def level_between(base: Version, target: Version) -> str:
    """O nível que separa ``base`` de ``target`` (o primeiro número que muda)."""
    if target.major != base.major:
        return "major"
    return "minor" if target.minor != base.minor else "patch"


def not_surpassed(candidate: Version, cycle: Iterable[Version]) -> Version | None:
    """A maior pré-release do mesmo núcleo que ``candidate`` não supera, ou ``None``.

    Uma versão nova tem de ter precedência maior que tudo que o ciclo já publicou para o mesmo
    alvo: ``1.3.0-alpha.1`` depois de ``1.3.0-beta.2`` regride. Em outro alvo a numeração recomeça.
    """
    same = [version for version in cycle if version.final == candidate.final and version.key() >= candidate.key()]
    return highest(same)
