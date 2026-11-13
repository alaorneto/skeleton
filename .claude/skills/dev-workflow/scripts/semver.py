"""Semantic Versioning 2.0.0: parsing, precedence, the next number and the 0.x rule (only the stdlib)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import total_ordering

LEVELS = ("none", "patch", "minor", "major")
_NUMBER = r"0|[1-9]\d*"
_IDENTIFIER = r"(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)"
PATTERN = re.compile(
    rf"(?P<major>{_NUMBER})\.(?P<minor>{_NUMBER})\.(?P<patch>{_NUMBER})"
    rf"(?:-(?P<pre>{_IDENTIFIER}(?:\.{_IDENTIFIER})*))?"
    r"(?:\+(?P<build>[0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?\Z"
)


class VersionError(ValueError):
    pass


@total_ordering
@dataclass(frozen=True)
class Version:
    major: int
    minor: int
    patch: int
    prerelease: tuple[str, ...] = ()
    build: tuple[str, ...] = ()

    @classmethod
    def parse(cls, text: str) -> Version:
        match = PATTERN.match(text.strip())
        if not match:
            raise VersionError(f"versão inválida para SemVer 2.0: {text!r}")
        pre, build = match.group("pre"), match.group("build")
        return cls(
            int(match.group("major")),
            int(match.group("minor")),
            int(match.group("patch")),
            tuple(pre.split(".")) if pre else (),
            tuple(build.split(".")) if build else (),
        )

    def __str__(self) -> str:
        text = f"{self.major}.{self.minor}.{self.patch}"
        if self.prerelease:
            text += "-" + ".".join(self.prerelease)
        if self.build:
            text += "+" + ".".join(self.build)
        return text

    @property
    def core(self) -> Version:
        return Version(self.major, self.minor, self.patch)

    @property
    def is_prerelease(self) -> bool:
        return bool(self.prerelease)

    def _key(self) -> tuple[object, ...]:
        # A pre-release has lower precedence than the release; build metadata never counts.
        identifiers = tuple((0, int(part), "") if part.isdigit() else (1, 0, part) for part in self.prerelease)
        return (self.major, self.minor, self.patch, 0 if self.prerelease else 1, identifiers)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Version) and self._key() == other._key()

    def __hash__(self) -> int:
        return hash(self._key())

    def __lt__(self, other: Version) -> bool:
        return self._key() < other._key()


def max_level(*levels: str) -> str:
    for level in levels:
        if level not in LEVELS:
            raise VersionError(f"nível desconhecido: {level!r}")
    return max(levels, key=LEVELS.index, default="none")


def effective_level(current: Version, level: str, *, explicit: bool = False) -> str:
    """The level applied to current: in 0.x a breaking change raises the minor, never 1.0.0.

    ``explicit`` (a ``semver:major`` label) is the one way to leave 0.x.
    """
    if current.major == 0 and level == "major" and not explicit:
        return "minor"
    return level


def bump(current: Version, level: str, *, prerelease: str | None = None) -> Version:
    """The next version for level; with prerelease (``rc``), ``X.Y.Z-rc.N`` counting up.

    From a pre-release of the same target, the release is the core version itself, and a new
    pre-release increments its counter.
    """
    if level not in LEVELS[1:]:
        raise VersionError(f"nível sem versão nova: {level!r}")
    base = current.core
    if current.is_prerelease:
        # 1.2.0-rc.1 + patch/minor/major targeting 1.2.0 stays on 1.2.0.
        target = base
        if level == "major" and (base.minor or base.patch):
            target = Version(base.major + 1, 0, 0)
        elif level == "minor" and base.patch:
            target = Version(base.major, base.minor + 1, 0)
    elif level == "major":
        target = Version(base.major + 1, 0, 0)
    elif level == "minor":
        target = Version(base.major, base.minor + 1, 0)
    else:
        target = Version(base.major, base.minor, base.patch + 1)
    if prerelease is None:
        return target
    if not re.fullmatch(r"[a-zA-Z][0-9a-zA-Z-]*", prerelease):
        raise VersionError(f"identificador de pré-release inválido: {prerelease!r}")
    counter = 1
    if current.is_prerelease and current.core == target and current.prerelease[0] == prerelease:
        last = current.prerelease[-1]
        counter = int(last) + 1 if last.isdigit() else 1
    return Version(target.major, target.minor, target.patch, (prerelease, str(counter)))
