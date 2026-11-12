"""Audit filter parsing (#184): allowlist, types, inclusive bounds, no database."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from django.core.exceptions import ValidationError
from django.http import QueryDict
from django.utils import timezone

from skeleton.audit.filters import parse_audit_filters


def test_valid_filters_become_service_keywords():
    actor, request = uuid4(), uuid4()
    with timezone.override("America/Sao_Paulo"):
        result = parse_audit_filters(
            QueryDict(
                f"from=2026-11-01&to=2026-11-02&actor={actor}&operation=update&request_id={request}"
                "&model_label=example_notes.note&page=2"
            )
        )
    assert result["actor_id"] == actor and result["request_id"] == request
    assert result["operation"] == "update" and result["model_label"] == "example_notes.note"
    # A date covers the whole day in the active time zone (UTC-3).
    assert result["occurred_from"].astimezone(UTC) == datetime(2026, 11, 1, 3, tzinfo=UTC)
    assert result["occurred_to"].astimezone(UTC) == datetime(2026, 11, 3, 2, 59, 59, 999999, tzinfo=UTC)


def test_datetime_bounds_keep_their_offset_and_empty_values_are_ignored():
    result = parse_audit_filters(QueryDict("from=2026-11-01T10:00:00-03:00&to=&actor="))
    assert result == {"occurred_from": datetime(2026, 11, 1, 13, tzinfo=UTC)}
    assert parse_audit_filters({}) == {}


@pytest.mark.parametrize(
    "query",
    [
        "tenant=x",
        "from=yesterday",
        "to=2026-13-01",
        "actor=nope",
        "request_id=1",
        "record_sysid=x",
        "operation=drop",
        "from=2026-11-02&to=2026-11-01",
        "operation=update&operation=create",
        "model_label=" + "x" * 256,
    ],
)
def test_invalid_or_unknown_filters_are_refused(query):
    with pytest.raises(ValidationError):
        parse_audit_filters(QueryDict(query))


def test_surface_decides_the_extra_parameters():
    with pytest.raises(ValidationError):
        parse_audit_filters(QueryDict("page_size=10"), allowed=("page",))
    assert parse_audit_filters(QueryDict("page=1"), allowed=("page",)) == {}
