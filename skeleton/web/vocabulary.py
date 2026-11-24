"""States and markers of apps in the visual vocabulary (#833), registered in code like job types.

The core reads closed maps (``STATUS`` and ``MARKERS`` in ``skeleton_format``); an app with
states of its own registers them here in ``AppConfig.ready()``, each code with its label and
tone, under a domain prefixed by its app label, and uses the filters as the core does:
``{{ appointment.state|status_badge:"med_scheduling.appointment" }}``. The tone keeps the one
meaning of the vocabulary (``docs/ui-design.md``); a code missing from the domain renders neutral.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from django.core.exceptions import ImproperlyConfigured
from django.utils.functional import Promise

__all__ = ["MARKER_TONES", "TONES", "app_markers", "app_states", "register_marker", "register_status"]

# success: working or done; brand: in progress; warning: needs attention or an action; danger:
# failed, blocked or irreversible; neutral: inactive, closed or draft. A marker is a fixed
# property, never "done": it has no success tone.
TONES = frozenset({"success", "brand", "warning", "danger", "neutral"})
MARKER_TONES = frozenset({"brand", "warning", "danger", "neutral"})

_DOMAIN = re.compile(r"(?P<app>[a-z][a-z0-9_]*)\.[a-z0-9_]{1,40}")
_CODE_MAX = 64

Entries = Mapping[str, tuple[Any, str]]
_states: dict[str, Mapping[str, tuple[Any, str]]] = {}
_markers: dict[str, Mapping[str, tuple[Any, str]]] = {}


def register_status(domain: str, states: Entries) -> None:
    """Register in ``AppConfig.ready()``: ``{code: (label, tone)}`` read by ``status_badge``."""
    _register(_states, "status", domain, states, TONES)


def register_marker(domain: str, markers: Entries) -> None:
    """Register in ``AppConfig.ready()``: ``{code: (label, tone)}`` read by ``marker``."""
    _register(_markers, "marker", domain, markers, MARKER_TONES)


def app_states(domain: str) -> Mapping[str, tuple[Any, str]] | None:
    """The states of an app's domain, or None when no app registered it."""
    return _states.get(domain)


def app_markers(domain: str) -> Mapping[str, tuple[Any, str]] | None:
    """The markers of an app's domain, or None when no app registered it."""
    return _markers.get(domain)


def unregister(domain: str) -> None:
    """Tests only: remove the states and markers of a domain (restore at the end of the test)."""
    _states.pop(domain, None)
    _markers.pop(domain, None)


def _register(
    registry: dict[str, Mapping[str, tuple[Any, str]]],
    kind: str,
    domain: str,
    entries: Entries,
    tones: frozenset[str],
) -> None:
    match = _DOMAIN.fullmatch(domain) if isinstance(domain, str) else None
    if match is None:
        raise ImproperlyConfigured(
            f"The {kind} domain {domain!r} must be '<app_label>.<name>' with name [a-z0-9_]{{1,40}}."
        )
    _require_installed(match["app"], kind, domain)
    if not isinstance(entries, Mapping) or not entries:
        raise ImproperlyConfigured(f"The {kind} domain {domain!r} needs a non-empty mapping of codes.")
    frozen: dict[str, tuple[Any, str]] = {}
    for code, entry in entries.items():
        if not isinstance(code, str) or not code or len(code) > _CODE_MAX:
            raise ImproperlyConfigured(f"The {kind} code {code!r} of {domain!r} must be a text of 1 to {_CODE_MAX}.")
        if not isinstance(entry, (tuple, list)) or len(entry) != 2:
            raise ImproperlyConfigured(f"The {kind} {domain}:{code} must be (label, tone).")
        label, tone = entry
        if not isinstance(label, (str, Promise)) or not str(label).strip():
            raise ImproperlyConfigured(f"The {kind} {domain}:{code} needs a label.")
        if tone not in tones:
            raise ImproperlyConfigured(
                f"The tone {tone!r} of the {kind} {domain}:{code} is not one of {', '.join(sorted(tones))}."
            )
        frozen[code] = (label, tone)
    existing = registry.get(domain)
    if existing is not None:
        if dict(existing) == frozen:
            return  # The same declaration loaded again (a second ready()); nothing changes.
        raise ImproperlyConfigured(f"The {kind} domain {domain!r} is already registered.")
    registry[domain] = MappingProxyType(frozen)


def _require_installed(app_label: str, kind: str, domain: str) -> None:
    from django.apps import apps

    if apps.apps_ready and app_label not in apps.app_configs:
        raise ImproperlyConfigured(f"The {kind} domain {domain!r} must start with the label of an installed app.")
