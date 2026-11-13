"""One classification of internal exceptions, shared by the REST, the service API and the forms (#325).

These characterization tables were written against the code before the refactoring: the REST
presentation and the service translation of every internal exception stay exactly the same.
"""

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError
from django.http import Http404

from skeleton.access.services import LastAdministrator
from skeleton.accounts.platform import LastPlatformAdministrator
from skeleton.common.exceptions import ConcurrentUpdate, LiveReferencesExist, MissingTenantContext
from skeleton.service_api import Conflict, ContextRequired, Forbidden, InvalidInput, NotFound
from tests.unit.test_service_api_base import ORGANIZATION, Sample, fake_context, person

UNIQUE = ValidationError({"code": ValidationError("dup", code="unique")})
FIELDS = "Verifique os campos informados."


@pytest.mark.parametrize(
    ("exc", "status", "code", "detail", "fields"),
    [
        (ConcurrentUpdate(), 412, "revision_conflict", "O registro foi alterado por outra operação.", None),
        (LiveReferencesExist(), 409, "live_references", "Existem vínculos ativos com este registro.", None),
        (
            LastAdministrator("x"),
            409,
            "last_administrator",
            "A organização precisa manter ao menos um administrador ativo.",
            None,
        ),
        (
            LastPlatformAdministrator(),
            409,
            "last_platform_administrator",
            "A plataforma precisa manter ao menos um administrador ativo.",
            None,
        ),
        (UNIQUE, 409, "integrity_conflict", FIELDS, {"code": ["dup"]}),
        (ValidationError({"title": ["Obrigatório."]}), 400, "validation_error", FIELDS, {"title": ["Obrigatório."]}),
        (ValidationError("x"), 400, "validation_error", FIELDS, {"non_field_errors": ["x"]}),
        (IntegrityError(), 409, "integrity_conflict", "A operação conflita com os dados existentes.", None),
    ],
    ids=lambda value: type(value).__name__ if isinstance(value, Exception) else None,
)
def test_rest_presentation_of_internal_errors_is_unchanged(exc, status, code, detail, fields):
    from skeleton.api.errors import exception_handler

    response = exception_handler(exc, {})
    assert response.status_code == status
    assert (response.data["code"], str(response.data["detail"])) == (code, detail)
    if fields is None:
        assert "fields" not in response.data
    else:
        assert response.data["fields"] == fields


def test_rest_leaves_unknown_and_context_errors_to_a_500():
    from skeleton.api.errors import exception_handler

    assert exception_handler(RuntimeError("x"), {}) is None
    assert exception_handler(MissingTenantContext(), {}) is None


@pytest.mark.parametrize(
    ("internal", "public", "code"),
    [
        (ConcurrentUpdate(), Conflict, "stale_revision"),
        (LiveReferencesExist(), Conflict, "live_references"),
        (LastAdministrator("x"), Conflict, "last_administrator"),
        (UNIQUE, Conflict, "integrity_conflict"),
        (ValidationError({"title": ["Obrigatório."]}), InvalidInput, "invalid_input"),
        (IntegrityError(), Conflict, "integrity_conflict"),
        (PermissionDenied(), Forbidden, "forbidden"),
        (Http404(), NotFound, "not_found"),
        (MissingTenantContext(), ContextRequired, "context_required"),
    ],
    ids=lambda value: type(value).__name__ if isinstance(value, Exception) else None,
)
def test_service_translation_of_internal_errors_is_unchanged(internal, public, code):
    actor = person()
    with fake_context(actor=actor.pk, organization_id=ORGANIZATION), pytest.raises(public) as raised:
        Sample().fail(actor=actor, exc=internal)
    assert raised.value.code == code
    if isinstance(raised.value, InvalidInput):
        assert dict(raised.value.errors) == {"title": ("Obrigatório.",)}


def test_service_lets_unknown_exceptions_propagate():
    actor = person()
    with fake_context(actor=actor.pk, organization_id=ORGANIZATION), pytest.raises(RuntimeError):
        Sample().fail(actor=actor, exc=RuntimeError("x"))


def test_last_platform_administrator_is_a_conflict_in_services():
    """The one new behaviour: it used to become InvalidInput, as a plain ValidationError (#325)."""
    actor = person()
    with fake_context(actor=actor.pk, organization_id=ORGANIZATION), pytest.raises(Conflict) as raised:
        Sample().fail(actor=actor, exc=LastPlatformAdministrator())
    assert raised.value.code == "last_platform_administrator"


def test_classify_returns_none_for_unknown_exceptions():
    from skeleton.service_api.internal_errors import classify

    assert classify(RuntimeError("x")) is None
    assert classify(PermissionDenied()) is None  # Each surface keeps its own answer to authorization.


def test_form_error_keeps_status_and_messages():
    from django import forms

    from skeleton.web.views import form_error

    class Form(forms.Form):
        code = forms.CharField(required=False)

    cases = [
        (LastAdministrator("x"), 409, "A organização precisa manter ao menos um administrador ativo."),
        (LastPlatformAdministrator(), 409, "A plataforma precisa manter ao menos um administrador ativo."),
        (UNIQUE, 409, "dup"),
        (ValidationError("x"), 400, "x"),
        (ConcurrentUpdate(), 409, "O registro foi alterado. Atualize a página antes de tentar novamente."),
        (LiveReferencesExist(), 409, "Existem vínculos ativos. Revogue-os antes de remover."),
        (IntegrityError(), 409, "A operação conflita com os dados existentes."),
    ]
    for error, status, message in cases:
        form = Form(data={})
        form.is_valid()
        assert form_error(form, error) == status, error
        assert message in str(form.errors), error
