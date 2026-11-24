"""The icon of a concept for templates (#768); the map itself is ``skeleton.common.icons``."""

from __future__ import annotations

from skeleton.common.icons import ACTION_ICONS, CONCEPT_ICONS

__all__ = ["ACTION_ICONS", "CONCEPT_ICONS", "concept_icon"]


def concept_icon(concept: str) -> str:
    """The icon of a concept; an unknown concept falls back to the neutral dot of icon.html."""
    return CONCEPT_ICONS.get(str(concept), "")
