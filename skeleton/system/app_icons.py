"""The icon of each app menu entry (#842): a concept of the core or a stroke icon the app ships.

A navigation entry of the manifest names ``icon`` (a concept of
``skeleton.common.icons.CONCEPT_ICONS``) or ``icon_svg`` (the path, inside the app package, of a
24×24 stroke icon). The SVG is read once per process, validated and rebuilt by
``skeleton.common.stroke_icons``; only that rebuilt geometry reaches a page, inside the core's own
``<svg>``. One concept, one icon: an SVG drawn like an icon of the core, or like the icon of
another app, is refused. A problem never breaks the menu: the entry keeps the ``apps`` icon and
``skeleton.E051`` names the app and the problem, never a path of the server.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from django.utils.safestring import SafeString, mark_safe

from skeleton.common.icons import CONCEPT_ICONS
from skeleton.common.stroke_icons import MAX_BYTES, StrokeIconError, stroke_icon_markup

__all__ = ["DEFAULT_CONCEPT", "EntryIcon", "entry_icon", "icon_problems"]

DEFAULT_CONCEPT = "apps"
_CORE_ICON = re.compile(r'\{% (?:el)?if name == "([a-z-]+)" %\}(.*?)(?=\{%|\n)')


@dataclass(frozen=True)
class EntryIcon:
    """What the menu draws: the concept whose icon to use, or the app's own stroke markup."""

    concept: str = DEFAULT_CONCEPT
    markup: SafeString | str = ""
    problem: str = ""


@lru_cache(maxsize=1)
def core_icon_markups() -> dict[str, str]:
    """The rebuilt geometry of every icon drawn by ``components/icon.html``, by markup."""
    import skeleton

    template = Path(skeleton.__file__).resolve().parent / "web/templates/skeleton/web/components/icon.html"
    markups: dict[str, str] = {}
    for name, inner in _CORE_ICON.findall(template.read_text(encoding="utf-8")):
        svg = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">{inner}</svg>'
        try:
            markups[stroke_icon_markup(svg.encode())] = name
        except StrokeIconError:
            continue  # A core icon outside the stroke subset cannot be matched by an app icon anyway.
    return markups


@lru_cache(maxsize=256)
def _svg(root: str, relative: str) -> tuple[str, str]:
    """(markup, problem) of one declared SVG; read once per process."""
    base = Path(root).resolve()
    path = (base / relative).resolve()  # A symbolic link out of the package is refused.
    if not path.is_relative_to(base) or not path.is_file():
        return "", "o arquivo do ícone não existe no pacote da app"
    if path.stat().st_size > MAX_BYTES:
        return "", f"o ícone passa de {MAX_BYTES} bytes"
    try:
        markup = stroke_icon_markup(path.read_bytes())
    except StrokeIconError as error:
        detail = f" ({error.item})" if error.item else ""
        return "", f"o SVG não é um ícone de traço 24×24 aceito: {error.code}{detail}"
    core = core_icon_markups().get(markup)
    if core:
        return "", f"o SVG é o ícone {core} do núcleo, que já representa outro conceito"
    return markup, ""


def _resolve(manifest, entry) -> EntryIcon:
    if entry.icon:
        if entry.icon not in CONCEPT_ICONS:
            return EntryIcon(problem=f"o conceito {entry.icon} não existe no núcleo")
        return EntryIcon(concept=entry.icon)
    if entry.icon_svg:
        if not manifest.root:
            return EntryIcon(problem="o pacote da app não foi localizado")
        markup, problem = _svg(manifest.root, entry.icon_svg)
        if problem:
            return EntryIcon(problem=problem)
        return EntryIcon(markup=mark_safe(markup))  # noqa: S308 - rebuilt geometry, values escaped.
    return EntryIcon()


def _shared_markups(manifests) -> dict[str, set[str]]:
    owners: dict[str, set[str]] = {}
    for manifest in manifests:
        for entry in manifest.navigation:
            icon = _resolve(manifest, entry)
            if icon.markup:
                owners.setdefault(str(icon.markup), set()).add(manifest.app_label)
    return {markup: labels for markup, labels in owners.items() if len(labels) > 1}


def entry_icon(manifest, entry, *, shared: dict[str, set[str]] | None = None) -> EntryIcon:
    """The icon of one menu entry; any problem falls back to the ``apps`` icon.

    ``shared`` is ``icons_shared_between_apps()`` when the caller draws several entries; an SVG
    two apps ship is drawn by neither (one concept, one icon).
    """
    icon = _resolve(manifest, entry)
    if icon.problem:
        return EntryIcon()
    if icon.markup and shared and str(icon.markup) in shared:
        return EntryIcon()
    return icon


def icons_shared_between_apps() -> dict[str, set[str]]:
    from .app_registry import all_apps

    return _shared_markups(all_apps())


def icon_problems() -> list[tuple[str, str, str]]:
    """(app label, entry label, problem) of every menu icon the menu cannot draw."""
    from .app_registry import all_apps

    manifests = list(all_apps())
    shared = _shared_markups(manifests)
    problems = []
    for manifest in manifests:
        for entry in manifest.navigation:
            icon = _resolve(manifest, entry)
            if icon.problem:
                problems.append((manifest.app_label, entry.label, icon.problem))
            elif icon.markup and str(icon.markup) in shared:
                others = ", ".join(sorted(shared[str(icon.markup)] - {manifest.app_label}))
                problems.append((manifest.app_label, entry.label, f"o SVG é o mesmo ícone da app {others}"))
    return problems
