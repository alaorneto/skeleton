"""Release packages in /platform/updates/ (#495): planned through the core package they carry."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.template.loader import render_to_string
from django.urls import reverse

from skeleton.accounts.platform import grant_platform_role
from skeleton.accounts.services import bootstrap_identity, create_identity
from skeleton.system.releases import plan_artifact
from skeleton.tenancy.context import request_context, system_context
from skeleton.web.platform import updates
from tests import support

pytestmark = pytest.mark.usefixtures("skeleton_db")
PASSWORD = "Release-package-Password!495"
RELEASE_DIGEST, CORE_DIGEST = "a" * 64, "b" * 64


def release_result(target="cp314-linux-x86_64"):
    return {
        "digest": RELEASE_DIGEST,
        "kind": "release",
        "manifest": {"kind": "release", "name": "Skeleton", "version": "0.6.0"},
        "core": {"digest": CORE_DIGEST, "manifest": {"kind": "core", "version": "0.6.0"}},
        "target": target,
        "agent": {"version": "0.6.0"},
    }


@pytest.fixture
def admin():
    user, _ = bootstrap_identity(
        email="release-package@example.test", organization="release-package", name="Release", password=PASSWORD
    )
    return user


def post_upload(client, monkeypatch, result):
    calls = []

    def upload(path):
        calls.append(path)
        return result

    monkeypatch.setattr(updates, "agent_client", lambda: SimpleNamespace(upload=upload))
    package = SimpleUploadedFile("release.skapp", b"PK\x05\x06" + b"\0" * 18, content_type="application/octet-stream")
    return client.post(reverse("platform:updates-upload"), {"package": package}), calls


def test_plan_artifact_of_each_kind():
    assert plan_artifact(release_result()) == (CORE_DIGEST, "core_update")
    assert plan_artifact({"digest": RELEASE_DIGEST, "kind": "app", "manifest": {}}) == (RELEASE_DIGEST, None)
    assert plan_artifact({"digest": RELEASE_DIGEST, "manifest": {}}) == (RELEASE_DIGEST, None)


def test_release_upload_opens_a_core_update_plan(admin, monkeypatch):
    client = support.login(admin, password=PASSWORD, csrf=False)
    response, calls = post_upload(client, monkeypatch, release_result())
    assert response.status_code == 200 and len(calls) == 1
    form = response.context["plan_form"]
    assert form.initial == {"artifacts": CORE_DIGEST, "action": "core_update"}
    body = response.content.decode()
    assert CORE_DIGEST in body
    assert "não traz dependências para a plataforma deste servidor" not in body


def test_release_without_this_target_warns_about_the_operator_lock(admin, monkeypatch):
    client = support.login(admin, password=PASSWORD, csrf=False)
    response, _ = post_upload(client, monkeypatch, release_result(target=None))
    assert response.status_code == 200
    assert "Valem o lock e o wheelhouse configurados no agente." in response.content.decode()


def test_app_upload_keeps_the_install_plan(admin, monkeypatch):
    client = support.login(admin, password=PASSWORD, csrf=False)
    app = {"digest": RELEASE_DIGEST, "kind": "app", "manifest": {"name": "Notes", "version": "1.0.0"}}
    response, _ = post_upload(client, monkeypatch, app)
    assert response.context["plan_form"].initial == {"artifacts": RELEASE_DIGEST, "action": "install"}


def test_upload_still_requires_the_release_capability(admin, monkeypatch):
    with system_context(reason="test:release-package-auditor"):
        auditor = create_identity(email="release-auditor@example.test", password=PASSWORD)
        from allauth.account.models import EmailAddress

        EmailAddress.objects.create(user=auditor, email=auditor.email, verified=True, primary=True)
    with request_context(actor=admin, request_id=uuid4(), source="test:release-package"):
        grant_platform_role(actor=admin, user=auditor, role="auditor")
    client = support.login(auditor, password=PASSWORD, csrf=False)
    response, calls = post_upload(client, monkeypatch, release_result())
    assert response.status_code == 403 and calls == []


def test_plan_details_list_dropped_dependencies():
    plan = {"components": [], "files": [], "resources": [], "presentation_changes": [], "migrations": []}
    assert "deixam de ser instaladas" not in render_to_string(
        "skeleton/web/platform/updates/plan-details.html", {"plan": plan}
    )
    text = render_to_string(
        "skeleton/web/platform/updates/plan-details.html", {"plan": {**plan, "dropped_dependencies": ["redis"]}}
    )
    assert "Dependências que deixam de ser instaladas" in text and "<code>redis</code>" in text


def agent_double(monkeypatch, *, self_update=True, calls=None):
    calls = [] if calls is None else calls

    def update_agent(digest, *, actor_id):
        calls.append((digest, actor_id))
        return {"state": "restarting", "version": "0.7.0"}

    inventory = {
        "generation": 1,
        "apps": [],
        "core": {"version": "0.6.0"},
        "agent": {"version": "0.6.0", "self_update": self_update},
    }
    client = SimpleNamespace(update_agent=update_agent, inventory=lambda: inventory, upload=lambda path: offer())
    from skeleton.system import releases

    monkeypatch.setattr(releases, "agent_client", lambda: client)
    monkeypatch.setattr(updates, "agent_client", lambda: client)
    return calls


def offer():
    return {**release_result(), "agent": {"version": "0.7.0", "update_required": True}}


def test_installed_versions_show_the_agent(admin, monkeypatch):
    agent_double(monkeypatch, self_update=False)
    client = support.login(admin, password=PASSWORD, csrf=False)
    body = client.get(reverse("platform:updates")).content.decode()
    assert "Agente de deployment" in body and "Atualização do agente: manual (veja o guia)" in body


def test_release_upload_offers_the_agent_update(admin, monkeypatch):
    agent_double(monkeypatch)
    client = support.login(admin, password=PASSWORD, csrf=False)
    package = SimpleUploadedFile("release.skapp", b"PK\x05\x06" + b"\0" * 18)
    body = client.post(reverse("platform:updates-upload"), {"package": package}).content.decode()
    assert "Atualizar agente" in body and reverse("platform:updates-agent") in body
    agent_double(monkeypatch, self_update=False)
    package = SimpleUploadedFile("release.skapp", b"PK\x05\x06" + b"\0" * 18)
    body = client.post(reverse("platform:updates-upload"), {"package": package}).content.decode()
    assert "Atualizar agente" not in body and "agent-install" in body


def test_agent_update_posts_the_digest_and_redirects(admin, monkeypatch):
    calls = agent_double(monkeypatch)
    client = support.login(admin, password=PASSWORD, csrf=False)
    response = client.post(reverse("platform:updates-agent"), {"release": RELEASE_DIGEST})
    assert response.status_code == 302 and response["Location"] == reverse("platform:updates")
    assert calls == [(RELEASE_DIGEST, admin.pk)]
    assert client.post(reverse("platform:updates-agent"), {"release": "../x"}).status_code == 400
    assert client.get(reverse("platform:updates-agent")).status_code == 405
    assert len(calls) == 1


def test_agent_update_needs_csrf_recent_authentication_and_the_capability(admin, monkeypatch):
    calls = agent_double(monkeypatch)
    strict = support.login(admin, password=PASSWORD)
    assert strict.post(reverse("platform:updates-agent"), {"release": RELEASE_DIGEST}).status_code == 403
    session = strict.session
    session["account_authentication_methods"][-1]["at"] = 0
    session.save()
    response = strict.post(
        reverse("platform:updates-agent"),
        {"release": RELEASE_DIGEST, "csrfmiddlewaretoken": strict.cookies["csrftoken"].value},
    )
    assert response.status_code == 302 and response["Location"].startswith(reverse("account_reauthenticate"))
    with system_context(reason="test:agent-update-auditor"):
        auditor = create_identity(email="agent-auditor@example.test", password=PASSWORD)
        from allauth.account.models import EmailAddress

        EmailAddress.objects.create(user=auditor, email=auditor.email, verified=True, primary=True)
    with request_context(actor=admin, request_id=uuid4(), source="test:agent-update"):
        grant_platform_role(actor=admin, user=auditor, role="auditor")
    denied = support.login(auditor, password=PASSWORD, csrf=False)
    assert denied.post(reverse("platform:updates-agent"), {"release": RELEASE_DIGEST}).status_code == 403
    assert calls == []


class SourcesAgent:
    """The agent's sources API, recorded; ``refuse`` makes set_source fail with that code."""

    def __init__(self, *, publishers=("skeleton", "acme"), rows=(), refuse=None):
        self.calls, self.refuse = [], refuse
        self.listing = {"sources": list(rows), "publishers": list(publishers), "private_available": False, "limit": 16}

    def sources(self):
        return self.listing

    def set_source(self, **values):
        self.calls.append(("set", values))
        if self.refuse:
            from skeleton.system.deployment_client import AgentError

            raise AgentError(self.refuse, 409)
        return self.listing

    def remove_source(self, source_id, *, actor_id):
        self.calls.append(("remove", source_id, actor_id))
        return self.listing


def sources_agent(monkeypatch, **options):
    from skeleton.system import releases

    double = SourcesAgent(**options)
    monkeypatch.setattr(releases, "agent_client", lambda: double)
    return double


def test_sources_page_suggests_the_upstream_repository(admin, monkeypatch):
    sources_agent(monkeypatch, rows=[{"id": "local", "type": "path", "channels": ["stable"], "origin": "config"}])
    client = support.login(admin, password=PASSWORD, csrf=False)
    response = client.get(reverse("platform:updates-sources"))
    assert response.status_code == 200
    form = response.context["source_form"]
    assert form.initial["repository"] == "alaorneto/skeleton" and form.initial["source_id"] == "skeleton"
    body = response.content.decode()
    assert "arquivo do agente" in body and "esta tela não cadastra chaves" in body
    assert [choice for choice, _ in form.fields["publisher"].choices] == ["skeleton", "acme"]
    assert "private" not in form.fields


def test_sources_page_without_publishers_explains_what_to_do(admin, monkeypatch):
    sources_agent(monkeypatch, publishers=())
    client = support.login(admin, password=PASSWORD, csrf=False)
    body = client.get(reverse("platform:updates-sources")).content.decode()
    assert "Nenhum publicador aprovado no servidor" in body


def test_save_and_remove_a_source(admin, monkeypatch):
    double = sources_agent(monkeypatch)
    client = support.login(admin, password=PASSWORD, csrf=False)
    data = {"source_id": "fork", "repository": "acme/skeleton", "publisher": "acme", "channel": "stable"}
    response = client.post(reverse("platform:updates-sources"), data)
    assert response.status_code == 302 and response["Location"] == reverse("platform:updates-sources")
    assert double.calls == [
        (
            "set",
            {
                "id": "fork",
                "github": "acme/skeleton",
                "publisher": "acme",
                "channels": ["stable"],
                "private": False,
                "actor_id": admin.pk,
            },
        )
    ]
    response = client.post(reverse("platform:updates-sources-remove"), {"source_id": "fork"})
    assert response.status_code == 302 and double.calls[-1] == ("remove", "fork", admin.pk)


@pytest.mark.parametrize(
    "changes",
    [
        {"repository": "https://github.com/acme/skeleton"},
        {"repository": "acme/skeleton/extra"},
        {"repository": "acme/../x"},
        {"publisher": "evil"},
        {"source_id": "Fork"},
    ],
)
def test_invalid_forms_never_reach_the_agent(admin, monkeypatch, changes):
    double = sources_agent(monkeypatch)
    client = support.login(admin, password=PASSWORD, csrf=False)
    data = {"source_id": "fork", "repository": "acme/skeleton", "publisher": "acme", "channel": "stable", **changes}
    assert client.post(reverse("platform:updates-sources"), data).status_code == 400
    assert double.calls == []


def test_agent_refusal_is_translated(admin, monkeypatch):
    sources_agent(monkeypatch, refuse="publisher_not_approved")
    client = support.login(admin, password=PASSWORD, csrf=False)
    data = {"source_id": "fork", "repository": "acme/skeleton", "publisher": "acme", "channel": "stable"}
    response = client.post(reverse("platform:updates-sources"), data, follow=True)
    assert "não está aprovado no servidor" in response.content.decode()


def test_unavailable_agent_shows_the_standard_notice(admin, monkeypatch):
    from skeleton.system import releases

    def unavailable():
        raise releases.DeploymentUnavailable("not configured")

    monkeypatch.setattr(releases, "agent_client", unavailable)
    client = support.login(admin, password=PASSWORD, csrf=False)
    response = client.get(reverse("platform:updates-sources"))
    assert response.status_code == 200 and "não está disponível" in response.content.decode()


def test_source_changes_need_csrf_recent_authentication_and_the_capability(admin, monkeypatch):
    double = sources_agent(monkeypatch)
    data = {"source_id": "fork", "repository": "acme/skeleton", "publisher": "acme", "channel": "stable"}
    strict = support.login(admin, password=PASSWORD)
    for name in ("platform:updates-sources", "platform:updates-sources-remove"):
        assert strict.post(reverse(name), data).status_code == 403
    session = strict.session
    session["account_authentication_methods"][-1]["at"] = 0
    session.save()
    response = strict.post(
        reverse("platform:updates-sources"), {**data, "csrfmiddlewaretoken": strict.cookies["csrftoken"].value}
    )
    assert response.status_code == 302 and response["Location"].startswith(reverse("account_reauthenticate"))
    with system_context(reason="test:sources-auditor"):
        auditor = create_identity(email="sources-auditor@example.test", password=PASSWORD)
        from allauth.account.models import EmailAddress

        EmailAddress.objects.create(user=auditor, email=auditor.email, verified=True, primary=True)
    with request_context(actor=admin, request_id=uuid4(), source="test:sources"):
        grant_platform_role(actor=admin, user=auditor, role="auditor")
    denied = support.login(auditor, password=PASSWORD, csrf=False)
    assert denied.get(reverse("platform:updates-sources")).status_code == 403
    assert denied.post(reverse("platform:updates-sources"), data).status_code == 403
    assert double.calls == []
