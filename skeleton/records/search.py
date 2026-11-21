"""Global search (#581): one term over every table marked with ``TableSpec.global_search``.

For each marked table the actor reads (``readable_specs``: TablePermission, token scopes and the
app enabled) it runs the table's own full-text search (``apply_search``, #180) on top of
``authorized_queryset`` and keeps the best ``limit`` rows: one query per table, each on the GIN
index the table's migration installed. No table of its own and no copy of business data. Ranks
are comparable inside one table only, so results come grouped by table.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from django.core.exceptions import ValidationError
from django.utils.translation import gettext

DEFAULT_LIMIT = 5
MAX_LIMIT = 20


@dataclass(frozen=True, slots=True)
class Hit:
    sysid: UUID
    label: str


@dataclass(frozen=True, slots=True)
class Group:
    table_key: str
    label: str
    hits: tuple[Hit, ...]
    has_more: bool


def global_search_declared() -> bool:
    """Whether any registered table opted in; no database access."""
    from skeleton.system.registry import all_specs

    return any(getattr(spec, "global_search", None) is not None for spec in all_specs())


def _label(spec: Any) -> str:
    return str(spec.label or spec.model._meta.verbose_name_plural)


def searchable_specs(user: Any) -> list[Any]:
    """Marked tables the actor reads, in the declared order, then by label and key."""
    from skeleton.records.services import readable_specs

    specs = [spec for spec in readable_specs(user) if spec.global_search is not None and spec.search_fields]
    return sorted(
        specs, key=lambda spec: (spec.global_search.order, _label(spec).casefold(), spec.model._meta.label_lower)
    )


def _limit(value: Any) -> int:
    if type(value) is not int or not 1 <= value <= MAX_LIMIT:
        raise ValidationError({"limit": gettext("Use um inteiro de 1 a 20.")})
    return value


def parse_request(params: Any) -> tuple[str, int]:
    """The same constrained request for HTML and REST: only ``q`` and ``limit``, each once."""
    from skeleton.system.search import clean_term

    if set(params) - {"q", "limit"}:
        raise ValidationError(gettext("Parâmetros inválidos."))
    if hasattr(params, "getlist") and any(len(params.getlist(key)) != 1 for key in params):
        raise ValidationError(gettext("Parâmetros repetidos."))
    term = clean_term(params.get("q"))
    raw = params.get("limit")
    if raw is None:
        return term, DEFAULT_LIMIT
    try:
        return term, _limit(int(raw))
    except (TypeError, ValueError):
        raise ValidationError({"limit": gettext("Use um inteiro de 1 a 20.")}) from None


def global_search(*, user: Any, term: Any, limit: int = DEFAULT_LIMIT) -> tuple[Group, ...]:
    """Best ``limit`` readable rows of each marked table; tables without a match are left out."""
    from skeleton.records.references import label_field, reference_label
    from skeleton.records.services import record_queryset
    from skeleton.system.search import apply_search, clean_term

    term = clean_term(term)
    limit = _limit(limit)
    groups = []
    for spec in searchable_specs(user):
        name = label_field(spec.model)
        queryset = apply_search(record_queryset(spec, user=user), spec, term)
        rows = list(queryset.only("sysid", *([name] if name else []))[: limit + 1])
        if not rows:
            continue
        groups.append(
            Group(
                table_key=spec.model._meta.label_lower,
                label=_label(spec),
                hits=tuple(Hit(sysid=row.pk, label=reference_label(row, user=user)) for row in rows[:limit]),
                has_more=len(rows) > limit,
            )
        )
    return tuple(groups)
