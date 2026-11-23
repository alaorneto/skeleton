"""The owning app of a core surface in the portal (#741): decided by the call chain, never by an argument."""

from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest

from skeleton.service_api import Forbidden, ServiceAPI, service_method
from skeleton.service_api import base as base_module
from skeleton.service_api.applications import require_portal_owner
from skeleton.service_api.base import portal_caller_app
from skeleton.tenancy import context as context_module

ORGANIZATION = uuid4()
PERSON = SimpleNamespace(pk=uuid4())


@contextmanager
def fake_context(**values):
    """Set the context variable directly: these tests exercise the decorator, not the database."""
    token = context_module._context.set(context_module.Context(**values))
    try:
        yield
    finally:
        context_module._context.reset(token)


def portal(**values):
    return fake_context(actor=PERSON.pk, organization_id=ORGANIZATION, external=True, **values)


class Probe(ServiceAPI):
    """A core surface: reports the app the portal rule sees, and applies it to an owner."""

    @service_method(read_only=True, since="0.6.0", allow_system=True, allow_external=True)
    def caller(self, *, actor) -> str:
        return portal_caller_app() or ""

    @service_method(read_only=True, since="0.6.0", allow_external=True)
    def owned_by(self, *, actor, owner: str) -> bool:
        require_portal_owner(owner, "Só a app dona.")
        return True


class NotesPortal(ServiceAPI):
    app_label = "example_notes"

    @service_method(read_only=True, since="0.6.0", allow_external=True)
    def ask(self, *, actor) -> str:
        return Probe().caller(actor=actor)

    @service_method(read_only=True, since="0.6.0", allow_external=True)
    def own(self, *, actor, owner: str) -> bool:
        return Probe().owned_by(actor=actor, owner=owner)

    @service_method(read_only=True, since="0.6.0", allow_external=True)
    def through_other(self, *, actor) -> str:
        return OtherPortal().ask(actor=actor)

    @service_method(read_only=True, since="0.6.0", allow_external=True)
    def then_ask(self, *, actor) -> tuple[str, str]:
        Probe().caller(actor=actor)
        return (Probe().caller(actor=actor), portal_caller_app() or "")


class NotesMember(ServiceAPI):
    app_label = "example_notes"

    @service_method(read_only=True, since="0.6.0")
    def ask(self, *, actor) -> str:
        return Probe().caller(actor=actor)


class OtherPortal(ServiceAPI):
    app_label = "other_app"

    @service_method(read_only=True, since="0.6.0", allow_external=True)
    def ask(self, *, actor) -> str:
        return Probe().caller(actor=actor)


class CorePortal(ServiceAPI):
    @service_method(read_only=True, since="0.6.0", allow_external=True)
    def ask(self, *, actor) -> str:
        return Probe().caller(actor=actor)


@pytest.fixture(autouse=True)
def providers(monkeypatch):
    """The app's availability is the decorator's own check, proved elsewhere; here every app is on."""
    monkeypatch.setattr(base_module, "_require_provider", lambda app_label: None)


def test_the_app_of_an_allow_external_method_is_the_caller_in_the_portal():
    with portal():
        assert NotesPortal().ask(actor=PERSON) == "example_notes"
        assert OtherPortal().ask(actor=PERSON) == "other_app"
        assert NotesPortal().through_other(actor=PERSON) == "other_app"  # The nearest caller decides.
        assert NotesPortal().then_ask(actor=PERSON) == ("example_notes", "")  # The chain unwinds after each call.


def test_no_app_outside_the_portal_or_without_an_app_method():
    with portal():
        assert Probe().caller(actor=PERSON) == ""  # A view or a test calling the surface directly.
        assert CorePortal().ask(actor=PERSON) == ""  # A core method belongs to no app.
    with fake_context(actor=PERSON.pk, organization_id=ORGANIZATION):
        assert NotesPortal().ask(actor=PERSON) == ""
        assert NotesMember().ask(actor=PERSON) == ""
    with fake_context(organization_id=ORGANIZATION, system=True):
        assert Probe().caller(actor=None) == ""
    assert portal_caller_app() is None


def test_a_method_without_the_mark_never_reaches_the_portal():
    with portal(), pytest.raises(Forbidden) as error:
        NotesMember().ask(actor=PERSON)
    assert error.value.code == "external_not_allowed"


def test_require_portal_owner():
    with portal():
        assert NotesPortal().own(actor=PERSON, owner="example_notes") is True
        for owner in ("other_app", ""):
            with pytest.raises(Forbidden) as error:
                NotesPortal().own(actor=PERSON, owner=owner)
            assert error.value.code != "external_not_allowed"
        with pytest.raises(Forbidden) as error:
            Probe().owned_by(actor=PERSON, owner="example_notes")
        assert error.value.code == "external_not_allowed"
    with fake_context(actor=PERSON.pk, organization_id=ORGANIZATION):
        assert Probe().owned_by(actor=PERSON, owner="anything") is True  # Members: nothing changes here.


def test_the_chain_unwinds_when_the_method_raises():
    class Failing(ServiceAPI):
        app_label = "example_notes"

        @service_method(read_only=True, since="0.6.0", allow_external=True)
        def fail(self, *, actor) -> None:
            raise RuntimeError("boom")

    with portal():
        with pytest.raises(RuntimeError):
            Failing().fail(actor=PERSON)
        assert Probe().caller(actor=PERSON) == ""
