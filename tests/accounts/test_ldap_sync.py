"""Directory sync of LDAP providers (#33), against ldap3's offline mock directory."""

import json
import uuid
from io import StringIO

import pytest
from allauth.socialaccount.models import SocialAccount
from cryptography.fernet import Fernet
from django.core.management import CommandError, call_command
from django.test import override_settings

from skeleton.accounts import identity_providers
from skeleton.accounts.ldap import client as ldap_client
from skeleton.accounts.providers import load_provider_config
from skeleton.accounts.services import bootstrap_identity, create_identity
from skeleton.tenancy.context import system_context

PASSWORD = "Ldap-sync-Password!33"
KEY = Fernet.generate_key().decode()
BASE = "dc=sync,dc=test"


class MockDirectory:
    def __init__(self):
        from ldap3 import MOCK_SYNC, Connection, Server

        self.server = Server("mock-sync")
        self.seed = Connection(self.server, user="cn=seed", password="seed", client_strategy=MOCK_SYNC)
        self.fail = None
        self.seed.strategy.add_entry("cn=svc," + BASE, {"userPassword": "svc-pass", "objectClass": "person"})

    def admin(self):
        from ldap3 import MOCK_SYNC, Connection

        connection = Connection(self.server, user="cn=svc," + BASE, password="svc-pass", client_strategy=MOCK_SYNC)
        assert connection.bind()
        return connection

    def add_person(self, uid):
        entry_uuid = str(uuid.uuid4())
        self.seed.strategy.add_entry(
            f"uid={uid},ou=people,{BASE}",
            {
                "uid": uid,
                "userPassword": "x",
                "mail": f"{uid}@sync.test",
                "entryUUID": entry_uuid,
                "objectClass": "person",
            },
        )
        return entry_uuid

    def remove_person(self, uid):
        assert self.admin().delete(f"uid={uid},ou=people,{BASE}")

    def connect(self, options, user, password):
        from ldap3 import MOCK_SYNC, Connection

        if self.fail:
            raise ldap_client.DirectoryError(self.fail)
        return Connection(self.server, user=user, password=password, client_strategy=MOCK_SYNC)


def empty():
    return load_provider_config(None)


def identity_with_account(uid_value, email, *, provider="sync-ldap"):
    with system_context(reason="test:sync-identity"):
        user = create_identity(email=email, password=None)
        SocialAccount.objects.create(user=user, provider=provider, uid=uid_value, extra_data={})
    return user


def member(organization, user, code="member"):
    from skeleton.access.models import MemberRole, Role
    from skeleton.tenancy.models import Membership

    with system_context(reason="test:sync-member", organization_id=organization.pk):
        membership = Membership.objects.create(organization=organization, user=user)
        MemberRole.objects.create(
            organization=organization, membership=membership, role=Role.objects.get(code=code, is_builtin=True)
        )
    return membership


def active(organization, user):
    from skeleton.tenancy.models import Membership

    with system_context(reason="test:sync-check", organization_id=organization.pk):
        return Membership.objects.filter(user=user, is_active=True).exists()


@pytest.fixture
def setup(skeleton_db):
    directory = MockDirectory()
    ldap_client.set_connection_factory(directory.connect)
    with override_settings(
        IDENTITY_PROVIDER_ENCRYPTION_KEYS=[KEY],
        SKELETON_PROVIDER_CONFIGURATION=empty(),
        SOCIALACCOUNT_PROVIDERS=empty().settings,
    ):
        owner, organization = bootstrap_identity(
            email="sync-owner@example.test", organization="sync-corp", name="Sync", password=PASSWORD
        )
        _, other = bootstrap_identity(
            email="sync-other@example.test", organization="sync-other", name="Other", password=PASSWORD
        )
        with system_context(reason="test:sync-provider"):
            row = identity_providers.create_identity_provider(
                actor=None,
                data={
                    "provider_id": "sync-ldap",
                    "protocol": "ldap",
                    "bound_organization": organization,
                    "directory": f"ldaps://ldap.sync.test/{BASE}",
                    "secret": "svc-pass",
                    "settings": {"bind_dn": "cn=svc," + BASE, "user_filter": "(uid={username})"},
                },
            )
        people = {}
        for name in ("ana", "bob", "carl"):
            people[name] = identity_with_account(directory.add_person(name), f"{name}@sync.test")
            member(organization, people[name])
        member(other, people["carl"])
        yield {"organization": organization, "other": other, "row": row, "people": people, "directory": directory}
    ldap_client.set_connection_factory(None)


def run(*args):
    out = StringIO()
    call_command("sync_directory", "--provider", "sync-ldap", "--format", "json", *args, stdout=out)
    return json.loads(out.getvalue())[0]


def test_dry_run_reports_and_writes_nothing(setup):
    setup["directory"].remove_person("carl")
    report = run("--dry-run")
    assert report["dry_run"] is True and report["deactivated"] == [str(setup["people"]["carl"].pk)]
    assert active(setup["organization"], setup["people"]["carl"])
    from skeleton.accounts.models import IdentityProvider

    assert IdentityProvider.objects.get(pk=setup["row"].pk).last_sync_code == ""


def test_sync_deactivates_only_in_the_bound_organization_and_is_idempotent(setup):
    from skeleton.accounts.models import IdentityProvider

    people, organization = setup["people"], setup["organization"]
    setup["directory"].remove_person("carl")
    report = run()
    assert report["deactivated"] == [str(people["carl"].pk)] and report["directory_users"] == 2
    assert not active(organization, people["carl"])
    assert active(setup["other"], people["carl"])  # Another organization is never touched.
    assert active(organization, people["ana"]) and active(organization, people["bob"])
    row = IdentityProvider.objects.get(pk=setup["row"].pk)
    assert row.last_sync_code == "ok" and row.last_sync_at is not None
    second = run()
    assert second["deactivated"] == [] and second["skipped_last_admin"] == []
    # Back in the directory: still inactive, a person decides a readmission.
    setup["directory"].add_person("carl")
    run()
    assert not active(organization, people["carl"])


def test_empty_directory_and_errors_abort_without_writing(setup):
    for name in ("ana", "bob", "carl"):
        setup["directory"].remove_person(name)
    with pytest.raises(CommandError):
        run()
    assert all(active(setup["organization"], user) for user in setup["people"].values())
    from skeleton.accounts.models import IdentityProvider

    assert IdentityProvider.objects.get(pk=setup["row"].pk).last_sync_code == "empty_directory"
    setup["directory"].add_person("ana")
    setup["directory"].fail = "unreachable"
    with pytest.raises(CommandError):
        run()
    assert all(active(setup["organization"], user) for user in setup["people"].values())


def test_too_many_deactivations_abort_the_organization(setup):
    setup["directory"].remove_person("bob")
    setup["directory"].remove_person("carl")
    with pytest.raises(CommandError):
        run("--max-deactivations", "1")
    assert active(setup["organization"], setup["people"]["bob"])
    assert active(setup["organization"], setup["people"]["carl"])


def test_last_administrator_is_skipped_and_reported(setup):
    from skeleton.access.models import MemberRole, Role
    from skeleton.tenancy.models import Membership

    organization, people = setup["organization"], setup["people"]
    with system_context(reason="test:sync-admin", organization_id=organization.pk):
        # Carl becomes the only administrator of the organization.
        MemberRole.objects.filter(role__code="organization_admin").exclude(membership__user=people["carl"]).delete()
        MemberRole.objects.create(
            organization=organization,
            membership=Membership.objects.get(user=people["carl"]),
            role=Role.objects.get(code="organization_admin", is_builtin=True),
        )
    setup["directory"].remove_person("carl")
    report = run()
    assert report["skipped_last_admin"] == [str(people["carl"].pk)] and report["deactivated"] == []
    assert active(organization, people["carl"])


def test_provision_admits_only_people_who_never_were_members(setup):
    from skeleton.tenancy.models import IdentityProviderAdmission, JoinRequest

    organization, row = setup["organization"], setup["row"]
    dave = identity_with_account(setup["directory"].add_person("dave"), "dave@sync.test")
    assert run()["provisioned"] == []  # --provision is off by default.
    assert run("--provision")["provisioned"] == []  # JIT off: nothing.
    with system_context(reason="test:sync-jit", organization_id=organization.pk):
        admission = IdentityProviderAdmission(organization=organization, provider=row, jit_provisioning=True)
        admission.save()
    report = run("--provision")
    assert report["provisioned"] == [str(dave.pk)] and active(organization, dave)
    # A former member is never provisioned again.
    setup["directory"].remove_person("bob")
    run()
    setup["directory"].add_person("bob")
    assert run("--provision")["provisioned"] == []
    with system_context(reason="test:sync-approval", organization_id=organization.pk):
        admission.refresh_from_db()
        admission.jit_requires_approval = True
        admission.save(update_fields={"jit_requires_approval"})
    erin = identity_with_account(setup["directory"].add_person("erin"), "erin@sync.test")
    assert run("--provision")["requested"] == [str(erin.pk)]
    with system_context(reason="test:sync-request", organization_id=organization.pk):
        assert JoinRequest.objects.get(user=erin).channel == "sso"
    assert not active(organization, erin)
