"""API tokens (#45): digest only, scopes, validity, revocation and auth_version."""

from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone

from skeleton.accounts.services import create_service_account
from skeleton.common.exceptions import ConcurrentUpdate
from skeleton.integrations.tokens import services
from skeleton.integrations.tokens.models import ApiToken
from skeleton.integrations.tokens.services import (
    TOKEN_PATTERN,
    issue_personal_token,
    issue_service_token,
    revoke_token,
    verify_token,
)
from skeleton.tenancy.context import request_context, system_context
from tools.security_fixtures import actor_tenant, build_security_fixture

pytestmark = pytest.mark.needs_example_app

NOTE_READ = "example_notes.note:read"


@pytest.fixture
def access(skeleton_db):
    return build_security_fixture()


def soon(days=30):
    return timezone.now() + timedelta(days=days)


def personal(user, scopes=(NOTE_READ,), **kwargs):
    with request_context(actor=user, request_id=uuid4(), source="test:tokens"):
        return issue_personal_token(actor=user, name="CLI", scopes=list(scopes), expires_at=soon(), **kwargs)


def test_only_the_digest_is_stored_and_the_audit_redacts_it(access):
    from skeleton.audit.models import AuditEvent

    issued = personal(access.alice)
    prefix, secret = TOKEN_PATTERN.match(issued.text).groups()
    assert len(prefix) == 8 and len(secret) == 43
    row = ApiToken.all_objects.filter(pk=issued.token.pk).values().get()
    assert secret not in repr(row) and row["digest"] != secret and row["prefix"] == prefix
    from skeleton.accounts.services import bootstrap_identity

    platform, _ = bootstrap_identity(
        email="token-audit@example.test", organization="token-audit", name="Audit", password="Token-audit-Pass-45!"
    )
    # Global events are readable only with the platform GUC on (RLS in PostgreSQL).
    with request_context(actor=platform, request_id=None, source="test:token-audit"):
        events = list(AuditEvent.objects.filter(record_sysid=issued.token.pk).values("changes", "actor_sysid"))
    assert events and secret not in repr(events) and row["digest"] not in repr(events)
    assert events[0]["actor_sysid"] == access.alice.pk
    assert verify_token(issued.text).pk == issued.token.pk


@pytest.mark.parametrize(
    "scopes",
    [
        [],
        ["nope"],
        ["sk_tenancy.membership:read"],  # Table that the generic API does not expose.
        ["example_notes.note:manage_access"],
        ["calendar.feed"],  # Unregistered named scope.
        "example_notes.note:read",
    ],
    ids=repr,
)
def test_unknown_or_unexposed_scopes_are_refused(access, scopes):
    with pytest.raises(ValidationError) as error:
        with request_context(actor=access.alice, request_id=uuid4(), source="test:tokens"):
            issue_personal_token(actor=access.alice, name="CLI", scopes=scopes, expires_at=soon())
    assert "scopes" in error.value.message_dict


def test_validity_is_mandatory_and_bounded(access, settings):
    settings.SKELETON_API_TOKEN_MAX_DAYS = 30
    for expires_at in (None, timezone.now() - timedelta(minutes=1), soon(31), soon(1).replace(tzinfo=None)):
        with pytest.raises(ValidationError) as error:
            with request_context(actor=access.alice, request_id=uuid4(), source="test:tokens"):
                issue_personal_token(actor=access.alice, name="CLI", scopes=[NOTE_READ], expires_at=expires_at)
        assert "expires_at" in error.value.message_dict
    assert personal(access.alice).token.expires_at <= soon(30)


def test_revocation_and_auth_version_invalidate_at_once(access, monkeypatch):
    from skeleton.accounts.models import User

    revoked = personal(access.alice)
    with request_context(actor=access.alice, request_id=uuid4(), source="test:tokens"):
        with pytest.raises(ConcurrentUpdate):
            revoke_token(actor=access.alice, token_id=revoked.token.pk, expected_revision=99)
        revoke_token(actor=access.alice, token_id=revoked.token.pk, expected_revision=revoked.token.revision)
    assert verify_token(revoked.text) is None
    row = ApiToken.all_objects.get(pk=revoked.token.pk)
    assert row.revoked_at is not None and row.deleted_at is not None
    bumped = personal(access.alice)
    assert verify_token(bumped.text) is not None
    with system_context(reason="test:token-auth-version"):
        user = User.objects.get(pk=access.alice.pk)
        user.auth_version += 1
        user.save(update_fields=["auth_version"])
    assert verify_token(bumped.text) is None
    expired = personal(access.bob)
    later = timezone.now() + timedelta(days=31)
    monkeypatch.setattr(services, "timezone", SimpleNamespace(now=lambda: later))
    assert verify_token(expired.text) is None
    monkeypatch.undo()
    assert verify_token(expired.text) is not None
    wrong = expired.text[:-4] + ("AAAA" if not expired.text.endswith("AAAA") else "BBBB")
    assert verify_token(wrong) is None
    assert verify_token("skt_short") is None and verify_token("") is None


def test_others_cannot_revoke_and_service_tokens_need_the_capability(access):
    issued = personal(access.alice)
    with request_context(actor=access.bob, request_id=uuid4(), source="test:tokens"):
        with pytest.raises(ApiToken.DoesNotExist):
            revoke_token(actor=access.bob, token_id=issued.token.pk, expected_revision=issued.token.revision)
    with actor_tenant(access.admin, access.a):
        account = create_service_account(actor=access.admin, name="ERP")
        service = issue_service_token(
            actor=access.admin, account=account, name="ERP", scopes=[NOTE_READ], expires_at=soon()
        )
    assert service.token.kind == "service" and service.token.bound_organization_id == access.a.pk
    with actor_tenant(access.alice, access.a):  # A member without integrations.manage.
        with pytest.raises(PermissionDenied):
            issue_service_token(actor=access.alice, account=account, name="x", scopes=[NOTE_READ], expires_at=soon())
        with pytest.raises(ApiToken.DoesNotExist):
            revoke_token(actor=access.alice, token_id=service.token.pk, expected_revision=service.token.revision)
    with actor_tenant(access.bob, access.b):  # Another organization's administrator.
        with pytest.raises(ApiToken.DoesNotExist):
            revoke_token(actor=access.bob, token_id=service.token.pk, expected_revision=service.token.revision)
        with pytest.raises(ValidationError):
            issue_service_token(actor=access.bob, account=account, name="x", scopes=[NOTE_READ], expires_at=soon())
    with pytest.raises(PermissionDenied):  # Service accounts never issue personal tokens.
        personal(account)


def test_mfa_policy_blocks_issue_without_enrolment(access, settings):
    settings.SKELETON_REQUIRE_ADMIN_MFA = True
    with pytest.raises(PermissionDenied):
        personal(access.alice)


def test_issue_is_refused_above_the_per_owner_limit(access, settings):
    """A live-token cap per owner; expired tokens count until revoked, revoking frees a slot (#327)."""
    settings.SKELETON_API_TOKEN_MAX_PER_OWNER = 2
    first = personal(access.alice)
    personal(access.alice)
    with pytest.raises(ValidationError) as error:
        personal(access.alice)
    assert "Limite de 2 tokens" in str(error.value)
    assert ApiToken.objects.filter(user=access.alice).count() == 2
    personal(access.bob)  # The cap is per owner.
    with request_context(actor=access.alice, request_id=uuid4(), source="test:tokens"):
        revoke_token(actor=access.alice, token_id=first.token.pk, expected_revision=first.token.revision)
    personal(access.alice)
    with actor_tenant(access.admin, access.a):
        account = create_service_account(actor=access.admin, name="ERP")
        for _ in range(2):
            issue_service_token(actor=access.admin, account=account, name="ERP", scopes=[NOTE_READ], expires_at=soon())
        with pytest.raises(ValidationError):
            issue_service_token(actor=access.admin, account=account, name="ERP", scopes=[NOTE_READ], expires_at=soon())
        assert ApiToken.objects.filter(user=account).count() == 2


def test_other_integrity_errors_are_not_reported_as_prefix_collisions(access, monkeypatch):
    """Only the prefix constraint means "draw again"; anything else is a real error (#327)."""
    from django.db import IntegrityError

    def broken(self, *args, **kwargs):
        raise IntegrityError('new row violates check constraint "outra_constraint"')

    monkeypatch.setattr(ApiToken, "save", broken)
    with pytest.raises(IntegrityError, match="outra_constraint"):
        personal(access.alice)
