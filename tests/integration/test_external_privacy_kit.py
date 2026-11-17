"""External accesses in the personal data rights and the portal contract kit (#536).

Runs on PostgreSQL with the runtime role.
"""

import io
import json
import zipfile
from contextlib import contextmanager

import pytest

from skeleton.service_api.base import ServiceAPI, service_method
from skeleton.service_api.external import ExternalAccessService
from skeleton.tenancy.context import system_context
from skeleton.testing.external import assert_external_contract, external_scenario

pytestmark = [pytest.mark.usefixtures("skeleton_db"), pytest.mark.needs_example_app]
APP = "example_notes"
LINKS: dict = {}


class FixturePortalService(ServiceAPI):
    """A fixture app's portal service: the items linked to the person's access."""

    app_label = APP

    @service_method(read_only=True, since="0.6.0", allow_external=True)
    def my_items(self, *, actor) -> tuple[str, ...]:
        """The items linked to the caller's external access.

        Autorização: só no portal, pela ligação da app ao acesso.
        Erros: Forbidden fora do portal.
        Efeitos: nenhum.
        """
        access = ExternalAccessService().me(actor=actor)
        return tuple(LINKS.get(access.sysid, ()))


@pytest.fixture
def scenario():
    from skeleton.testing.applications import activate_app, open_app

    found = external_scenario("ext-privacy")
    open_app(APP)
    for organization in (found.a, found.b):
        activate_app(APP, organization.pk)
    return found


def test_the_export_lists_the_persons_external_accesses(scenario):
    from skeleton.accounts.privacy import export_my_data

    with zipfile.ZipFile(io.BytesIO(export_my_data(scenario.external))) as archive:
        data = json.loads(archive.read("dados.json"))
    [access] = data["external_accesses"]
    assert access["organization"]["sysid"] == str(scenario.a.pk) and access["is_active"] is True
    assert set(access) == {"organization", "app_label", "is_active", "created_at", "last_seen_at"}
    assert scenario.dual.email not in json.dumps(data)


def test_erasure_closes_the_access_keeps_it_and_a_new_invitation_needs_a_new_acceptance(
    scenario, settings, django_capture_on_commit_callbacks
):
    import re
    from uuid import UUID

    from allauth.account.models import EmailAddress
    from django.core import mail
    from django.core.exceptions import PermissionDenied

    from skeleton.accounts.erasure import forget_in_organization
    from skeleton.tenancy import external
    from skeleton.tenancy.context import external_context
    from skeleton.tenancy.models import ExternalAccess
    from skeleton.testing.service_api import acting_as

    with system_context(reason="test:erasure", organization_id=scenario.a.pk):
        forget_in_organization(user_id=str(scenario.external.pk))
        [access] = ExternalAccess.objects.filter(user=scenario.external)
    assert not access.is_active
    with pytest.raises(PermissionDenied):
        with external_context(organization_id=scenario.a.pk, actor=scenario.external):
            pass
    settings.SKELETON_PUBLIC_URL = "https://portal.example.test"
    with acting_as(scenario.admin_a, scenario.a) as admin, django_capture_on_commit_callbacks(execute=True):
        ExternalAccessService().invite(
            actor=admin, email=scenario.external.email, app_label=APP, subject_ref=UUID(int=9), idempotency_key="again"
        )
    token = re.search(r"/portal/invite/[0-9a-f-]+/([A-Za-z0-9_-]+)/", mail.outbox[-1].body).group(1)
    with system_context(reason="test:verified-email"):
        EmailAddress.objects.get_or_create(
            user=scenario.external, email=scenario.external.email, defaults={"verified": True}
        )
    again = external.accept(user=scenario.external, organization_id=scenario.a.pk, token=token)
    assert again.pk == access.pk and again.is_active  # The same history row, reactivated by the new acceptance.


def test_the_kit_proves_the_portal_contract_of_an_app_method(scenario):
    from skeleton.system.applications import application_state, configure_application
    from skeleton.testing.service_api import acting_as

    with system_context(reason="test:links", organization_id=scenario.a.pk):
        from skeleton.tenancy.models import ExternalAccess

        LINKS[ExternalAccess.objects.get(user=scenario.external).pk] = ["item-1"]

    @contextmanager
    def disabled():
        def configure(enabled):
            with acting_as(scenario.admin_a, scenario.a):
                state = application_state(APP)
                configure_application(
                    APP,
                    user=scenario.admin_a,
                    enabled=enabled,
                    configuration=state["configuration"],
                    revision=state["revision"],
                )

        configure(False)
        try:
            yield
        finally:
            configure(True)

    assert_external_contract(
        FixturePortalService,
        "my_items",
        scenario=scenario,
        call=lambda actor: FixturePortalService().my_items(actor=actor),
        unlinked=lambda result: result == (),
        disabled=disabled,
    )


def test_the_kit_catches_a_method_without_the_mark(scenario):
    class Unmarked(ServiceAPI):
        @service_method(read_only=True, since="0.6.0")
        def items(self, *, actor) -> tuple[str, ...]:
            """Items.

            Autorização: nenhuma.
            Erros: nenhum.
            Efeitos: nenhum.
            """
            return ()

    with pytest.raises(AssertionError, match="allow_external"):
        assert_external_contract(Unmarked, "items", scenario=scenario, call=lambda actor: Unmarked().items(actor=actor))
