"""Text search on declared fields (#180) and in the reference picker (#181).

``TableSpec.search_fields`` names the text columns a table searches. The migration of the table
installs ``InstallSearchIndex``: a GIN index over the expression

    to_tsvector('portuguese', skeleton.immutable_unaccent(coalesce(f1, '') || ' ' || ...))

and, for the picker, an index over ``skeleton.immutable_unaccent(<label>)`` (trigram when
``pg_trgm`` is installed, ``text_pattern_ops`` for prefixes otherwise). The query applies the
same expression with ``plainto_tsquery``, on top of ``authorized_queryset`` and before the
pagination, so the count and the pages only ever hold what the reader may see.

``skeleton.immutable_unaccent`` is a plain SQL function created by ``sk_system`` migration
0008 (``translate`` over the Latin accented letters): no extension is needed for the full-text
search. ``pg_trgm`` is optional; without it the picker matches prefixes only and the
deployment check warns.
"""

from __future__ import annotations

from typing import Any

from django.core.exceptions import ValidationError
from django.db.models import BooleanField, FloatField
from django.db.models.expressions import RawSQL
from django.utils.translation import gettext

UNACCENT = "skeleton.immutable_unaccent"
CONFIG = "portuguese"
MIN_TERM = 2
MAX_TERM = 200
ACCENTED = "áàâãäåéèêëíìîïóòôõöúùûüçñýÿÁÀÂÃÄÅÉÈÊËÍÌÎÏÓÒÔÕÖÚÙÛÜÇÑÝ"
PLAIN = "aaaaaaeeeeiiiiooooouuuucnyyAAAAAAEEEEIIIIOOOOOUUUUCNY"


def unaccent_function_sql() -> str:
    """DDL of the immutable unaccent function (lower-cased, accents removed)."""
    return (
        f"CREATE OR REPLACE FUNCTION {UNACCENT}(value text) RETURNS text "
        "LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT "
        f"AS $$ SELECT translate(lower(value), '{ACCENTED}', '{PLAIN}') $$"
    )


def _column(model: Any, name: str, *, qualified: bool) -> str:
    column = model._meta.get_field(name).column
    if qualified:
        return f'"{model._meta.db_table}"."{column}"'
    return f'"{column}"'


def vector_sql(model: Any, fields: tuple[str, ...], *, qualified: bool = True) -> str:
    joined = " || ' ' || ".join(f"coalesce({_column(model, name, qualified=qualified)}, '')" for name in fields)
    return f"to_tsvector('{CONFIG}'::regconfig, {UNACCENT}({joined}))"


def label_sql(model: Any, name: str, *, qualified: bool = True) -> str:
    return f"{UNACCENT}({_column(model, name, qualified=qualified)})"


def clean_term(term: Any) -> str:
    if not isinstance(term, str):
        raise ValidationError({"q": gettext("Informe um texto.")})
    term = " ".join(term.split())
    if len(term) < MIN_TERM or len(term) > MAX_TERM:
        raise ValidationError({"q": gettext("Use de 2 a 200 caracteres.")})
    return term


def apply_search(queryset: Any, spec: Any, term: Any) -> Any:
    """Full-text filter and rank on ``spec.search_fields``; the queryset is already authorized."""
    fields = tuple(getattr(spec, "search_fields", ()) or ())
    if not fields:
        raise ValidationError({"q": gettext("Esta tabela não tem busca.")})
    term = clean_term(term)
    model = queryset.model
    vector = vector_sql(model, fields)
    query = f"plainto_tsquery('{CONFIG}'::regconfig, {UNACCENT}(%s))"
    return (
        queryset.alias(_sk_match=RawSQL(f"{vector} @@ {query}", (term,), output_field=BooleanField()))
        .filter(_sk_match=True)
        .annotate(_sk_rank=RawSQL(f"ts_rank({vector}, {query})", (term,), output_field=FloatField()))
        .order_by("-_sk_rank", "created_at", "sysid")
    )


def trigram_available(using: str = "default") -> bool:
    from django.db import connections

    with connections[using].cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'")
        return cursor.fetchone() is not None


def apply_label_search(queryset: Any, label: str | None, term: Any) -> Any:
    """Picker filter on the label: prefix always, similarity (typos) when ``pg_trgm`` exists."""
    term = clean_term(term)
    if label is None:
        return queryset.none()
    model = queryset.model
    expression = label_sql(model, label)
    if trigram_available(queryset.db):
        return (
            queryset.alias(
                _sk_label=RawSQL(
                    f"({expression} LIKE {UNACCENT}(%s) || '%%' OR {expression} %% {UNACCENT}(%s))",
                    (term, term),
                    output_field=BooleanField(),
                )
            )
            .filter(_sk_label=True)
            .annotate(
                _sk_similarity=RawSQL(f"similarity({expression}, {UNACCENT}(%s))", (term,), output_field=FloatField())
            )
            .order_by("-_sk_similarity", "created_at", "sysid")
        )
    return queryset.alias(
        _sk_label=RawSQL(f"{expression} LIKE {UNACCENT}(%s) || '%%'", (term,), output_field=BooleanField())
    ).filter(_sk_label=True)


def search_problems(using: str = "default") -> tuple[list[str], list[str]]:
    """(errors, warnings) of the deployed search infrastructure, for ``check_deployed_schema``."""
    from django.db import connections

    from skeleton.system.registry import all_specs

    errors: list[str] = []
    warnings: list[str] = []
    with connections[using].cursor() as cursor:
        cursor.execute(
            "SELECT p.provolatile FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = 'skeleton' AND p.proname = 'immutable_unaccent'"
        )
        row = cursor.fetchone()
        if row is None or row[0] != "i":
            errors.append("skeleton.immutable_unaccent is missing or not IMMUTABLE.")
        for spec in all_specs():
            fields = tuple(getattr(spec, "search_fields", ()) or ())
            if not fields:
                continue
            table = spec.model._meta.db_table
            cursor.execute(
                "SELECT indexdef FROM pg_indexes WHERE schemaname = 'skeleton' AND indexname = %s",
                [f"{table}_sk_search"],
            )
            found = cursor.fetchone()
            columns = [spec.model._meta.get_field(name).column for name in fields]
            if found is None or "to_tsvector" not in found[0] or any(column not in found[0] for column in columns):
                errors.append(f"{table}: search index is missing or does not match search_fields.")
    if not trigram_available(using):
        warnings.append("pg_trgm is not installed: the reference picker matches prefixes only.")
    return errors, warnings
