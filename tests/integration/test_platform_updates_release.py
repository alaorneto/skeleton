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
