"""The brand of the instance (#592): CAS, platform administrator only, audit and the process cache."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, connections, transaction

from skeleton.common.exceptions import ConcurrentUpdate
from tests.integration.appearance_support import acting, audit_events, member, platform_admin

pytestmark = pytest.mark.usefixtures("skeleton_db", "live_appearance")


def brand_row():
    from skeleton.appearance.services import current_brand

    return current_brand()


def test_the_administrator_creates_and_changes_the_brand_with_audit():
    from skeleton.appearance.services import update_brand
    from skeleton.appearance.snapshot import DEFAULT, current_appearance

    admin = platform_admin()
    with acting(admin):
        assert current_appearance() == DEFAULT
        created = update_brand(actor=admin, data={"product_name": " Acme Cloud "}, expected_revision=0)
    assert created.product_name == "Acme Cloud" and created.revision == 1
    with acting(admin):
        assert current_appearance().product_name == "Acme Cloud"  # Reset after the commit, same process.
        changed = update_brand(actor=admin, data={"footer_text": "Feito com cuidado"}, expected_revision=1)
    with acting(admin):
        assert current_appearance().footer_text == "Feito com cuidado"
    assert changed.revision == 2
    events = audit_events(admin, created.pk)
    assert [event.operation for event in events] == ["create", "update"]
    assert all(event.actor_sysid == admin.pk for event in events)
    assert events[0].model_label == "sk_appearance.instancebrand"


def test_equal_data_writes_nothing():
    from skeleton.appearance.services import update_brand

    admin = platform_admin()
    with acting(admin):
        record = update_brand(actor=admin, data={"product_name": "Acme"}, expected_revision=0)
        again = update_brand(actor=admin, data={"product_name": "Acme "}, expected_revision=record.revision)
    assert again.revision == record.revision == 1
    assert len(audit_events(admin, record.pk)) == 1


def test_anyone_else_is_refused():
    from skeleton.appearance.services import update_brand

    platform_admin()
    other = member()
    with acting(other), pytest.raises(PermissionDenied):
        update_brand(actor=other, data={"product_name": "Tomado"}, expected_revision=0)
    with acting(platform_admin()):
        assert brand_row() is None


def test_wrong_or_missing_revision_is_a_concurrent_update(subtests):
    from skeleton.appearance.services import update_brand

    admin = platform_admin()
    with acting(admin):
        update_brand(actor=admin, data={"product_name": "Acme"}, expected_revision=0)
        for revision in (0, 2, "1", None):
            with subtests.test(revision=revision), pytest.raises(ConcurrentUpdate):
                update_brand(actor=admin, data={"product_name": "Outro"}, expected_revision=revision)
        assert brand_row().product_name == "Acme"


def test_invalid_fields_name_the_field(subtests):
    from skeleton.appearance.services import update_brand

    admin = platform_admin()
    cases = [
        ({"logo_key": "x"}, "logo_key"),
        ({"product_name": "Duas\nlinhas"}, "product_name"),
        ({"footer_text": "a\rb"}, "footer_text"),
        ({"product_name": "nulo\x00"}, "product_name"),
        ({"product_name": "x" * 61}, "product_name"),
        ({"footer_text": "x" * 161}, "footer_text"),
        ({"product_name": 7}, "product_name"),
    ]
    with acting(admin):
        for data, field in cases:
            with subtests.test(field=field, data=repr(data)[:30]):
                with pytest.raises(ValidationError) as error:
                    update_brand(actor=admin, data=data, expected_revision=0)
                assert field in error.value.message_dict
        assert brand_row() is None


def test_a_rollback_leaves_no_row_nor_event():
    from skeleton.appearance.services import update_brand
    from skeleton.audit.models import AuditEvent

    admin = platform_admin()
    with acting(admin):
        with pytest.raises(RuntimeError), transaction.atomic():
            update_brand(actor=admin, data={"product_name": "Desfeito"}, expected_revision=0)
            raise RuntimeError("rollback")
        assert brand_row() is None
        assert not AuditEvent.objects.filter(model_label="sk_appearance.instancebrand").exists()


def test_two_writers_with_the_same_revision_have_one_winner():
    from skeleton.appearance.services import update_brand

    connection.ensure_connection()
    admin = platform_admin()
    with acting(admin):
        update_brand(actor=admin, data={"product_name": "Base"}, expected_revision=0)
    barrier = Barrier(2)

    def write(name):
        connections.close_all()
        try:
            with acting(admin):
                barrier.wait(10)
                try:
                    update_brand(actor=admin, data={"product_name": name}, expected_revision=1)
                except ConcurrentUpdate:
                    return "lost"
                return "won"
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = sorted(pool.map(write, ["Um", "Dois"], timeout=60))
    assert results == ["lost", "won"]
    with acting(admin):
        assert brand_row().revision == 2


def test_the_command_shows_the_brand_without_keys(capsys):
    from django.core.management import call_command

    from skeleton.appearance.services import update_brand

    admin = platform_admin()
    with acting(admin):
        update_brand(actor=admin, data={"product_name": "Acme", "footer_text": "Rodapé"}, expected_revision=0)
    call_command("appearance", "show")
    output = capsys.readouterr().out
    assert "Nome do produto: Acme" in output and "Rodapé: Rodapé" in output
    assert "Logo: não" in output and "Tema ativo: núcleo" in output
    assert "platform/branding" not in output
