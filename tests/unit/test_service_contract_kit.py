"""The service contract kit against correct and deliberately defective services (#100)."""

import enum
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID, uuid4

import pytest

from skeleton.service_api import Conflict, Forbidden, NotFound, Page, ServiceAPI, Unavailable, service_method
from skeleton.testing.service_api import (
    assert_cas_conflict,
    assert_denied_as_not_found,
    assert_rejects_other_actor,
    assert_service_contract,
    assert_tenant_isolated,
    assert_unavailable_when_disabled,
)

DOC = """Documented.

Autorização: read na tabela.
Erros: NotFound.
Efeitos: nenhum.
"""


class Status(enum.StrEnum):
    OPEN = "open"


@dataclass(frozen=True, slots=True)
class Item:
    sysid: UUID
    title: str
    status: Status
    amount: Decimal | None
    tags: tuple[str, ...]
    at: datetime


@dataclass(frozen=True, slots=True)
class Wrapper:
    item: Item
    kind: Literal["a", "b"]


@dataclass(slots=True)
class Mutable:
    sysid: UUID


@dataclass(frozen=True, slots=True)
class HoldsList:
    values: list[str]


def method(returns: Any, *, doc: str = DOC, annotated: bool = True):
    def function(self, *, actor, page_size: int = 25):
        return None

    function.__doc__ = doc
    function.__annotations__ = {"page_size": int, "return": returns} if annotated else {}
    return service_method(read_only=True, since="1.0")(function)


def service(**methods):
    return type("Generated", (ServiceAPI,), methods)


@pytest.mark.parametrize(
    "returns",
    [int, str, bool, UUID, None, Item, Wrapper, Page[Item], tuple[Item, ...], Item | None, Status, tuple[int, str]],
)
def test_valid_return_types_pass(returns):
    assert_service_contract(service(read=method(returns)))


@pytest.mark.parametrize(
    ("returns", "fragment"),
    [
        (dict, "not a DTO"),
        (list[Item], "not a DTO"),
        (Any, "not a DTO"),
        (Page, "item type"),
        (Mutable, "frozen"),
        (HoldsList, "HoldsList.values"),
        (float, "not a DTO"),
        (Item | dict, "not a DTO"),
    ],
)
def test_invalid_return_types_fail(returns, fragment):
    with pytest.raises(AssertionError, match=fragment):
        assert_service_contract(service(read=method(returns)))


def test_service_returning_a_model_fails():
    from django.contrib.auth.models import Group

    with pytest.raises(AssertionError, match="Group"):
        assert_service_contract(service(read=method(Group)))


def test_missing_annotations_and_sections_fail():
    with pytest.raises(AssertionError) as raised:
        assert_service_contract(service(read=method(int, doc="Only a summary.", annotated=False)))
    message = str(raised.value)
    assert "return type must be annotated" in message
    assert "'page_size' requires a type annotation" in message
    for section in ("Autorização", "Erros", "Efeitos"):
        assert f"'{section}:'" in message


def test_service_with_positional_argument_fails():
    def positional(self, actor):
        return 1

    with pytest.raises(AssertionError, match="without @service_method"):
        assert_service_contract(service(read=method(int), positional=positional))
    with pytest.raises(TypeError, match="keyword-only"):
        service_method(read_only=True, since="1.0")(positional)


def test_unresolvable_return_annotation_is_reported():
    with pytest.raises(AssertionError, match="cannot be resolved"):
        assert_service_contract(service(read=method("MissingDTO")))


def raise_(error):
    def call():
        raise error

    return call


def test_tenant_isolation_detects_leaks_in_results_pages_and_errors():
    foreign_id = uuid4()
    leaked = Item(foreign_id, "Foreign", Status.OPEN, None, (), datetime(2026, 1, 1))
    with pytest.raises(AssertionError, match="leaks"):
        assert_tenant_isolated(lambda: Page(items=(leaked,), next_cursor=None, page_size=1), foreign=[foreign_id])
    with pytest.raises(AssertionError, match="leaks"):
        assert_tenant_isolated(lambda: ("x", "contains Foreign title"), foreign=["Foreign"])
    with pytest.raises(AssertionError, match="error reveals"):
        assert_tenant_isolated(raise_(NotFound(f"no {foreign_id}")), foreign=[foreign_id])
    own = Item(uuid4(), "Own", Status.OPEN, None, (), datetime(2026, 1, 1))
    assert assert_tenant_isolated(lambda: Page(items=(own,), next_cursor=None, page_size=1), foreign=[foreign_id])
    assert isinstance(assert_tenant_isolated(raise_(NotFound()), foreign=[foreign_id]), NotFound)
    with pytest.raises(TypeError):
        assert_tenant_isolated(lambda: 1, foreign=[42])


def test_denied_must_look_like_missing():
    assert_denied_as_not_found(raise_(NotFound()), missing=raise_(NotFound()))
    with pytest.raises(AssertionError, match="differently"):
        assert_denied_as_not_found(raise_(NotFound("Sem acesso.")), missing=raise_(NotFound()))
    with pytest.raises(AssertionError, match="got Forbidden"):
        assert_denied_as_not_found(raise_(Forbidden()))
    with pytest.raises(AssertionError, match="returned"):
        assert_denied_as_not_found(lambda: "visible")


def test_unavailable_other_actor_and_cas_helpers():
    entered = []

    @contextmanager
    def disabled():
        entered.append(True)
        yield

    assert_unavailable_when_disabled(raise_(Unavailable()), disabled=disabled)
    assert entered == [True]
    with pytest.raises(AssertionError):
        assert_unavailable_when_disabled(lambda: 1, disabled=disabled)
    assert_rejects_other_actor(raise_(Forbidden()))
    with pytest.raises(AssertionError):
        assert_rejects_other_actor(lambda: 1)
    assert assert_cas_conflict(lambda: "first", raise_(Conflict("stale_revision"))) == "first"
    with pytest.raises(AssertionError, match="stale_revision"):
        assert_cas_conflict(lambda: "first", raise_(Conflict("integrity_conflict")))
    with pytest.raises(AssertionError):
        assert_cas_conflict(lambda: "first", lambda: "second wins too")
