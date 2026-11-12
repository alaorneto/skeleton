"""LDAP login per organization through allauth (#32), against ldap3's offline mock directory."""

import pytest
from cryptography.fernet import Fernet
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.test import Client, override_settings
from django.urls import reverse

from skeleton.accounts import identity_providers
from skeleton.accounts.ldap import client as ldap_client
from skeleton.accounts.providers import load_provider_config
from skeleton.accounts.services import bootstrap_identity, create_identity
from skeleton.tenancy.context import system_context, tenant_context

PASSWORD = "Ldap-login-Password!32"
KEY = Fernet.generate_key().decode()
BASE = "dc=ex,dc=test"
ANA_UUID = "6f1c4b6e-6c1e-4f4e-9d2b-1b2c3d4e5f60"
DIRECTORY = f"ldaps://ldap.example.test/{BASE}"


class MockDirectory:
    def __init__(self):
        from ldap3 import MOCK_SYNC, Connection, Server

        self.server = Server("mock-directory")
        self.seed = Connection(self.server, user="cn=seed", password="seed", client_strategy=MOCK_SYNC)
        self.binds = []
        self.add("cn=svc," + BASE, userPassword="svc-pass", objectClass="person")
        self.add_person("ana", ANA_UUID, "ana@corp.example.test", "ana-pass")

    def add(self, dn, **attributes):
        self.seed.strategy.add_entry(dn, attributes)

    def add_person(self, uid, entry_uuid, mail, password):
        self.add(
            f"uid={uid},ou=people,{BASE}",
            uid=uid,
            userPassword=password,
            mail=mail,
            entryUUID=entry_uuid,
            givenName=uid.title(),
            sn="Directory",
            objectClass="person",
        )

    def set_mail(self, uid, mail):
        from ldap3 import MOCK_SYNC, MODIFY_REPLACE, Connection

        admin = Connection(self.server, user="cn=svc," + BASE, password="svc-pass", client_strategy=MOCK_SYNC)
        assert admin.bind()
        assert admin.modify(f"uid={uid},ou=people,{BASE}", {"mail": [(MODIFY_REPLACE, [mail])]})

    def connect(self, options, user, password):
        from ldap3 import MOCK_SYNC, Connection

        self.binds.append(user)
        return Connection(self.server, user=user, password=password, client_strategy=MOCK_SYNC)


@pytest.fixture
def directory():
    mock = MockDirectory()
    ldap_client.set_connection_factory(mock.connect)
    cache.clear()
    yield mock
    ldap_client.set_connection_factory(None)
    cache.clear()


def empty():
    return load_provider_config(None)


@pytest.fixture
def setup(skeleton_db, directory):
    from skeleton.tenancy.models import IdentityProviderAdmission

    with override_settings(
        IDENTITY_PROVIDER_ENCRYPTION_KEYS=[KEY],
        SKELETON_PROVIDER_CONFIGURATION=empty(),
        SOCIALACCOUNT_PROVIDERS=empty().settings,
    ):
        owner, organization = bootstrap_identity(
            email="ldap-owner@example.test", organization="ldap-corp", name="Corp", password=PASSWORD
        )
        with system_context(reason="test:ldap-provider"):
            row = identity_providers.create_identity_provider(
                actor=None,
                data={
                    "provider_id": "corp-ldap",
                    "protocol": "ldap",
                    "bound_organization": organization,
                    "directory": DIRECTORY,
                    "name": "Diretório Corp",
                    "secret": "svc-pass",
                    "trust_verified_email": True,
                    "settings": {"bind_dn": "cn=svc," + BASE, "user_filter": "(uid={username})"},
                },
            )
        with system_context(reason="test:ldap-admission", organization_id=organization.pk):
            IdentityProviderAdmission(organization=organization, provider=row, jit_provisioning=True).save()
        yield {"owner": owner, "organization": organization, "row": row, "directory": directory}


def url():
    return reverse("ldap_login", kwargs={"provider_id": "corp-ldap"})


def sign_in(client, username, password):
    return client.post(url(), {"username": username, "password": password})


def test_login_creates_an_sso_only_identity_with_the_directory_uuid(setup):
    from allauth.socialaccount.models import SocialAccount

    from skeleton.accounts.models import User
    from skeleton.tenancy.models import Membership

    client = Client()
    assert client.get(url()).status_code == 200
    response = sign_in(client, "ana", "ana-pass")
    assert response.status_code == 302, response.content
    user = User.objects.get(email="ana@corp.example.test")
    assert user.local_password_allowed is False and not user.has_usable_password()
    account = SocialAccount.objects.get(user=user)
    assert (account.provider, account.uid) == ("corp-ldap", ANA_UUID)
    assert account.extra_data == {"skeleton_binding": setup["row"].fingerprint}
    with tenant_context(organization_id=setup["organization"].pk, actor=setup["owner"], source="test:ldap-member"):
        assert Membership.objects.filter(user=user, is_active=True).exists()
    # The directory changes the email: the same identity comes back, no new user.
    setup["directory"].set_mail("ana", "ana.new@corp.example.test")
    again = Client()
    assert sign_in(again, "ana", "ana-pass").status_code == 302
    assert SocialAccount.objects.filter(provider="corp-ldap").count() == 1
    assert User.objects.filter(email__startswith="ana").count() == 1


def test_matching_local_email_never_connects_by_itself(setup):
    with system_context(reason="test:ldap-local"):
        create_identity(email="ana@corp.example.test", password=PASSWORD)
    response = sign_in(Client(), "ana", "ana-pass")
    assert response.status_code == 403 and b"Identity admission denied." in response.content


def test_failures_are_neutral_and_escape_the_filter(setup):
    directory = setup["directory"]
    client = Client()
    wrong = sign_in(client, "ana", "wrong-pass")
    unknown = sign_in(client, "nobody", "whatever")
    injected = sign_in(client, "*)(uid=*", "ana-pass")
    for response in (wrong, unknown, injected):
        assert response.status_code == 401 and "Usuário ou senha inválidos.".encode() in response.content
    # The injected filter matched nobody: only the service account bound for that attempt.
    assert directory.binds.count("uid=ana,ou=people," + BASE) == 1
    before = len(directory.binds)
    assert sign_in(client, "ana", "").status_code == 400  # Empty password never reaches a bind.
    assert len(directory.binds) == before
    with pytest.raises(ldap_client.InvalidCredentials):
        ldap_client.authenticate(
            ldap_client.options_from(DIRECTORY, {"bind_dn": "cn=svc," + BASE}), "svc-pass", "ana", ""
        )
    assert len(directory.binds) == before


def test_rate_limit_answers_429(setup, monkeypatch):
    from skeleton.accounts.ldap import views

    monkeypatch.setattr(views, "USER_LIMIT", (2, 600))
    client = Client()
    for _ in range(2):
        assert sign_in(client, "ana", "wrong").status_code == 401
    limited = sign_in(client, "ana", "ana-pass")
    assert limited.status_code == 429 and limited["Retry-After"] == "600"


def test_tls_is_mandatory_in_the_configuration(setup):
    organization = setup["organization"]
    base = {
        "provider_id": "plain-ldap",
        "protocol": "ldap",
        "bound_organization": organization,
        "secret": "x",
        "settings": {"bind_dn": "cn=svc," + BASE},
    }
    for directory, extra in (
        (f"ldap://ldap.example.test/{BASE}", {}),
        ("ldaps://ldap.example.test/", {}),
        (f"ldaps://ldap.example.test/{BASE}", {"user_filter": "uid={username}"}),
        (f"ldaps://ldap.example.test/{BASE}", {"id_attribute": "mail"}),
        (f"ldaps://ldap.example.test/{BASE}", {"verify": False}),
    ):
        data = {**base, "directory": directory, "settings": {**base["settings"], **extra}}
        with system_context(reason="test:ldap-tls"), pytest.raises(ValidationError):
            identity_providers.create_identity_provider(actor=None, data=data)
    with system_context(reason="test:ldap-starttls"):
        row = identity_providers.create_identity_provider(
            actor=None,
            data={
                **base,
                "directory": f"ldap://ldap.example.test/{BASE}",
                "settings": {**base["settings"], "start_tls": True},
            },
        )
    assert row.directory.startswith("ldap://")


def test_disabled_provider_and_object_guid(setup):
    import uuid

    row = setup["row"]
    with system_context(reason="test:ldap-disable"):
        identity_providers.update_identity_provider(
            actor=None, provider_id=row.pk, data={"enabled": False}, expected_revision=row.revision
        )
    assert Client().get(url()).status_code == 404
    guid = uuid.uuid4()
    entry = {"attributes": {"objectGUID": guid.bytes_le}}
    assert ldap_client._uid(entry["attributes"]["objectGUID"], "objectGUID") == str(guid)


def test_platform_registers_and_tests_an_ldap_provider(setup):
    from skeleton.accounts.models import IdentityProvider

    setup["directory"].add(BASE, objectClass="domain")
    platform = Client()
    owner = setup["owner"]  # bootstrap_identity makes the owner a platform administrator.
    assert platform.post(reverse("account_login"), {"login": owner.email, "password": PASSWORD}).status_code == 302
    create = reverse("platform:identity-provider-create")
    page = platform.get(create + "?protocol=ldap")
    assert page.status_code == 200 and b'name="directory"' in page.content and b'name="client_id"' not in page.content
    form = {
        "protocol": "ldap",
        "provider_id": "corp-ldap-2",
        "name": "Outro diretório",
        "bound_organization": str(setup["organization"].pk),
        "directory": f"ldaps://ldap2.example.test/{BASE}",
        "secret": "svc-pass",
        "bind_dn": "cn=svc," + BASE,
        "user_filter": "(uid={username})",
        "id_attribute": "entryUUID",
    }
    assert platform.post(create, {**form, "directory": f"ldap://ldap2.example.test/{BASE}"}).status_code == 400
    response = platform.post(create, form)
    assert response.status_code == 302, response.content
    row = IdentityProvider.objects.get(provider_id="corp-ldap-2")
    assert row.secret.reveal() == "svc-pass" and b"svc-pass" not in platform.get(response["Location"]).content
    assert platform.post(reverse("platform:identity-provider-check", args=[row.pk])).status_code == 302
    assert IdentityProvider.objects.get(pk=row.pk).last_check_code == "ok"
