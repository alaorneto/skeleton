"""Release admin: dependency problems, catalogue suggestions and the add-to-plan flow."""

from types import SimpleNamespace

import pytest
from django.test import Client

from skeleton.accounts.services import bootstrap_identity
from skeleton.system.deployment_client import AgentError, sanitize_details
from skeleton.system.models import DeploymentOperation

pytestmark = pytest.mark.usefixtures("skeleton_db")
PASSWORD = "Release-dependency-password!31"
BASE_DIGEST = "b" * 64
CONSUMER_DIGEST = "c" * 64


def platform_client():
    user, _ = bootstrap_identity(
        email="release-dependencies@example.test", organization="release-deps", name="Deps", password=PASSWORD
    )
    client = Client()
    assert client.post("/accounts/login/", {"login": user.email, "password": PASSWORD}).status_code == 302
    return client


MISSING = {
    "code": "missing_dependency",
    "component": "org.test.consumer",
    "dependency": "org.test.base",
    "required": ">=1.0",
    "suggestions": [{"source_id": "main", "component_id": "org.test.base", "version": "1.2.0", "digest": BASE_DIGEST}],
}


def test_client_keeps_only_the_closed_problem_shape():
    details = sanitize_details(
        [
            {**MISSING, "secret": "never", "suggestions": [{**MISSING["suggestions"][0], "url": "https://x"}]},
            {"code": "dependency_cycle", "component": "a", "path": ["a", "b", "a"]},
            {"component": "missing-code"},
            "not-a-dict",
            {"code": "x" * 101, "component": "too-long-code"},
        ]
    )
    assert details == (
        {**MISSING},
        {"code": "dependency_cycle", "component": "a", "path": ["a", "b", "a"]},
    )
    assert sanitize_details({"code": "x"}) == ()


def test_preview_failure_explains_the_problem_and_offers_the_suggestion(monkeypatch):
    from skeleton.system import releases

    def preview(action, artifacts=None, app_ids=None):
        raise AgentError("dependency_unsatisfied", 409, sanitize_details([MISSING]))

    monkeypatch.setattr(releases, "agent_client", lambda: SimpleNamespace(preview=preview))
    response = platform_client().post(
        "/admin/system/releases/preview/", {"action": "install", "artifacts": CONSUMER_DIGEST}
    )
    assert response.status_code == 409
    page = response.content.decode()
    assert "org.test.consumer exige org.test.base &gt;=1.0" in page
    assert "Adicionar ao plano" in page
    assert f'name="artifacts" value="{CONSUMER_DIGEST}"' in page
    assert not DeploymentOperation.objects.exists()


@pytest.mark.parametrize("upgrade, expected", [("", "install"), ("1", "update")])
def test_add_dependency_fetches_and_previews_the_combined_plan(monkeypatch, upgrade, expected):
    from skeleton.system import release_admin, releases

    calls = []

    def fetch_release(component_id, version, source_id):
        calls.append(("fetch", component_id, version, source_id))
        return {"digest": BASE_DIGEST, "manifest": {}}

    def preview(action, artifacts=None, app_ids=None):
        calls.append(("preview", action, artifacts))
        return {"id": "plan", "action": action, "generation": 3, "digest": "d" * 64}

    agent = SimpleNamespace(fetch_release=fetch_release, preview=preview)
    monkeypatch.setattr(release_admin, "agent_client", lambda: agent)
    monkeypatch.setattr(releases, "agent_client", lambda: agent)
    response = platform_client().post(
        "/admin/system/releases/add-dependency/",
        {
            "component_id": "org.test.base",
            "version": "1.2.0",
            "source_id": "main",
            "action": "install",
            "artifacts": CONSUMER_DIGEST,
            "upgrade": upgrade,
        },
    )
    assert response.status_code == 302
    assert calls == [
        ("fetch", "org.test.base", "1.2.0", "main"),
        ("preview", expected, [CONSUMER_DIGEST, BASE_DIGEST]),
    ]
    assert DeploymentOperation.objects.get().operation == expected


def test_add_dependency_rejects_an_uninstall_plan(monkeypatch):
    from skeleton.system import release_admin

    monkeypatch.setattr(release_admin, "agent_client", lambda: pytest.fail("Invalid plan contacted the agent."))
    response = platform_client().post(
        "/admin/system/releases/add-dependency/",
        {"component_id": "org.test.base", "version": "1.2.0", "source_id": "main", "action": "uninstall"},
    )
    assert response.status_code == 400


@pytest.mark.parametrize(
    "component, installed, expected",
    [
        ("org.test.new", [], "install"),
        ("org.test.old", ["org.test.old"], "update"),
        ("django-skeleton", [], "core_update"),
    ],
)
def test_fetched_release_preselects_the_matching_action(monkeypatch, component, installed, expected):
    from skeleton.system import release_admin

    agent = SimpleNamespace(
        fetch_release=lambda **values: {"digest": BASE_DIGEST, "manifest": {"name": component, "version": "1.0.0"}},
        inventory=lambda: {"apps": [{"manifest": {"app_id": app_id}, "state": "installed"} for app_id in installed]},
    )
    monkeypatch.setattr(release_admin, "agent_client", lambda: agent)
    response = platform_client().post(
        "/admin/system/releases/fetch/", {"component_id": component, "version": "1.0.0", "source_id": "main"}
    )
    assert response.status_code == 200
    assert f'<option value="{expected}" selected>' in response.content.decode()
