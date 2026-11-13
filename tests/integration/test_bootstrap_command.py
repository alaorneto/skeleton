"""bootstrap_skeleton: idempotent first administrator; the password never comes from the CLI (#335)."""

import io

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

pytestmark = pytest.mark.usefixtures("skeleton_db")
FIRST = "Bootstrap-fixture-Phrase-335!"
SECOND = "Another-fixture-Phrase-335!"


def bootstrap():
    out = io.StringIO()
    call_command(
        "bootstrap_skeleton", "--email", "Root@Example.Test ", "--organization", "boot", "--name", "Boot", stdout=out
    )
    return out.getvalue()


def root():
    from skeleton.accounts.models import User

    return User.objects.filter(email="root@example.test").first()


def test_bootstrap_creates_platform_admin_and_organization_from_environment(monkeypatch):
    from skeleton.tenancy.context import system_context
    from skeleton.tenancy.models import Organization

    monkeypatch.setenv("SKELETON_BOOTSTRAP_PASSWORD", FIRST)
    output = bootstrap()
    user = root()
    assert user is not None and user.is_staff and user.is_superuser
    with system_context(reason="test:bootstrap-org"):
        organization = Organization.objects.get(slug="boot")
    assert str(user.pk) in output and str(organization.pk) in output
    assert FIRST not in output


def test_bootstrap_is_idempotent_and_never_resets_the_password(monkeypatch):
    monkeypatch.setenv("SKELETON_BOOTSTRAP_PASSWORD", FIRST)
    first = bootstrap()
    monkeypatch.setenv("SKELETON_BOOTSTRAP_PASSWORD", SECOND)
    assert bootstrap() == first
    user = root()
    assert user.check_password(FIRST)
    assert not user.check_password(SECOND)


def test_bootstrap_without_password_and_without_tty_is_refused(monkeypatch):
    monkeypatch.delenv("SKELETON_BOOTSTRAP_PASSWORD", raising=False)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    with pytest.raises(CommandError, match="SKELETON_BOOTSTRAP_PASSWORD"):
        bootstrap()
    assert root() is None


def test_bootstrap_interactive_mismatch_is_refused(monkeypatch):
    answers = iter([FIRST, SECOND])
    terminal = io.StringIO("")
    terminal.isatty = lambda: True
    monkeypatch.delenv("SKELETON_BOOTSTRAP_PASSWORD", raising=False)
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(answers))
    monkeypatch.setattr("sys.stdin", terminal)
    with pytest.raises(CommandError, match="Passwords do not match."):
        bootstrap()
    assert root() is None
