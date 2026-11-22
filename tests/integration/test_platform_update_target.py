"""The core version the operator chooses in /platform/updates/ (#653): the steps toward it, one
preview and confirmation each, and the form value never trusted."""

from uuid import uuid4

import pytest
from django.urls import reverse

from skeleton.accounts.platform import grant_platform_role
from skeleton.accounts.services import bootstrap_identity, create_identity
from skeleton.tenancy.context import request_context, system_context
from skeleton.web.platform import updates
from tests import support
from tests.integration.test_platform_updates_release import (
    PASSWORD,
    finished_operation,
    path_agent,
    release_fetch,
)

pytestmark = pytest.mark.usefixtures("skeleton_db")


@pytest.fixture
def admin():
    user, _ = bootstrap_identity(
        email="update-target@example.test", organization="update-target", name="Target", password=PASSWORD
    )
    return user


def step(version):
    return {"version": version, "source_id": "official"}


CORE = {
    "component_id": "django-skeleton",
    "kind": "core",
    "installed": "0.3.0",
    "latest": "0.5.0",
    "steps": [step("0.4.2"), step("0.5.0")],
    "blocked": None,
    "targets": [
        {"version": "0.3.1", "steps": [step("0.3.1")]},
        {"version": "0.4.2", "steps": [step("0.4.2")]},
        {"version": "0.5.0", "steps": [step("0.4.2"), step("0.5.0")]},
    ],
}


def status(**core):
    return {"checked_at": "2026-11-22T12:00:00+00:00", "sources": [], "releases": [], "paths": [{**CORE, **core}]}


def core_card(response):
    return next(card for card in response.context["paths"] if card["component_id"] == "django-skeleton")


def fetch(client, version, target=None):
    data = {"component_id": "django-skeleton", "version": version, "source_id": "official"}
    if target is not None:
        data["target"] = target
    return client.post(reverse("platform:updates-fetch"), data)


def fetched(double):
    return [call for call in double.calls if isinstance(call, tuple) and call[0] == "fetch_release"]


def test_the_page_offers_the_reachable_targets_and_the_steps_to_the_chosen_one(admin, monkeypatch, subtests):
    path_agent(monkeypatch, status=status())
    client = support.login(admin, password=PASSWORD, csrf=False)
    url = reverse("platform:updates-available")
    with subtests.test("targets, the newest marked"):
        body = client.get(url).content.decode()
        for version in ("0.3.1", "0.4.2"):
            assert f'<option value="{version}">{version}</option>' in body
        assert '<option value="0.5.0">0.5.0 (mais nova)</option>' in body
        assert '<option value="0.4.0"' not in body
    with subtests.test("choosing 0.5.0 shows 0.4.2 then 0.5.0, only the first with a button"):
        response = client.get(url, {"target": "0.5.0"})
        body = response.content.decode()
        assert [item["version"] for item in core_card(response)["steps"]] == ["0.4.2", "0.5.0"]
        assert body.count("Preparar este passo") == 1 and "Depois: 0.5.0" in body
        assert 'name="target" value="0.5.0"' in body and 'name="version" value="0.4.2"' in body
    with subtests.test("the choice stays for the session only after a step is prepared"):
        assert core_card(client.get(url))["target"] is None
    with subtests.test("a nearer target"):
        response = client.get(url, {"target": "0.3.1"})
        assert core_card(response)["steps"] == [step("0.3.1")]
    with subtests.test("a target that is not offered shows the refusal and no step"):
        response = client.get(url, {"target": "0.4.0"})
        body = response.content.decode()
        assert core_card(response)["steps"] == [] and "deixou de ser alcançável" in body
        assert "Preparar este passo" not in body


def test_a_step_toward_the_target_is_fetched_and_remembered(admin, monkeypatch):
    double = path_agent(monkeypatch, status=status(), fetched=release_fetch("0.4.2"))
    client = support.login(admin, password=PASSWORD, csrf=False)
    response = fetch(client, "0.4.2", "0.5.0")
    assert response.status_code == 200 and response.context["plan_form"].initial["action"] == "core_update"
    assert fetched(double) == [
        ("fetch_release", {"component_id": "django-skeleton", "version": "0.4.2", "source_id": "official"})
    ]
    assert client.session[updates.TARGET_SESSION_KEY] == "0.5.0"
    assert core_card(client.get(reverse("platform:updates-available")))["target"] == "0.5.0"
    fetch(client, "0.4.2")  # A step of the default path forgets the choice.
    assert updates.TARGET_SESSION_KEY not in client.session


def test_forged_or_stale_targets_never_reach_the_agent(admin, monkeypatch, subtests):
    for case, version, target, current in (
        ("not offered", "0.4.2", "0.4.0", status()),
        ("not the first step toward it", "0.5.0", "0.5.0", status()),
        ("no longer reachable", "0.4.2", "0.5.0", status(targets=[{"version": "0.3.1", "steps": [step("0.3.1")]}])),
        ("too long", "0.4.2", "9" * 65, status()),
    ):
        with subtests.test(case=case):
            double = path_agent(monkeypatch, status=current, fetched=release_fetch("0.4.2"))
            client = support.login(admin, password=PASSWORD, csrf=False)
            response = fetch(client, version, target)
            assert response.status_code == 302 and response["Location"] == reverse("platform:updates-available")
            assert fetched(double) == [] and updates.TARGET_SESSION_KEY not in client.session
            body = client.get(response["Location"]).content.decode()
            assert "deixou de ser alcançável" in body


def test_choosing_a_target_needs_csrf_recent_authentication_and_the_capability(admin, monkeypatch):
    double = path_agent(monkeypatch, status=status(), fetched=release_fetch("0.4.2"))
    data = {"component_id": "django-skeleton", "version": "0.4.2", "source_id": "official", "target": "0.5.0"}
    strict = support.login(admin, password=PASSWORD)
    assert strict.post(reverse("platform:updates-fetch"), data).status_code == 403
    session = strict.session
    session["account_authentication_methods"][-1]["at"] = 0
    session.save()
    response = strict.post(
        reverse("platform:updates-fetch"), {**data, "csrfmiddlewaretoken": strict.cookies["csrftoken"].value}
    )
    assert response.status_code == 302 and response["Location"].startswith(reverse("account_reauthenticate"))
    with system_context(reason="test:update-target-auditor"):
        auditor = create_identity(email="target-auditor@example.test", password=PASSWORD)
        from allauth.account.models import EmailAddress

        EmailAddress.objects.create(user=auditor, email=auditor.email, verified=True, primary=True)
    with request_context(actor=admin, request_id=uuid4(), source="test:update-target"):
        grant_platform_role(actor=admin, user=auditor, role="auditor")
    denied = support.login(auditor, password=PASSWORD, csrf=False)
    assert denied.post(reverse("platform:updates-fetch"), data).status_code == 403
    assert denied.get(reverse("platform:updates-available"), {"target": "0.5.0"}).status_code == 403
    assert fetched(double) == []


def test_after_each_step_the_next_toward_the_target_until_it_is_reached(admin, monkeypatch, subtests):
    row = finished_operation(admin)
    double = path_agent(monkeypatch, status=status(), fetched=release_fetch("0.4.2"))
    double.operation = lambda operation_id: {
        "id": str(row.pk),
        "state": "succeeded",
        "stage": "ready",
        "generation": 2,
        "expected_generation": 1,
        "plan_digest": row.plan_digest,
        "operation": row.operation,
        "action": row.operation,
    }
    client = support.login(admin, password=PASSWORD, csrf=False)
    fetch(client, "0.4.2", "0.5.0")
    url = reverse("platform:updates-operation", args=[row.pk])
    with subtests.test("the next step toward the target"):
        double.status = status(
            installed="0.4.2",
            steps=[step("0.5.0"), step("0.6.1")],
            latest="0.6.1",
            targets=[
                {"version": "0.5.0", "steps": [step("0.5.0")]},
                {"version": "0.6.1", "steps": [step("0.5.0"), step("0.6.1")]},
            ],
        )
        body = client.get(url).content.decode()
        assert "Próximo passo" in body and 'name="version" value="0.5.0"' in body
        assert 'name="target" value="0.5.0"' in body and "Depois: 0.6.1" not in body
    with subtests.test("a target no longer reachable is refused, not replaced"):
        double.status = status(installed="0.4.2", steps=[step("0.6.1")], latest="0.6.1", targets=[])
        body = client.get(url).content.decode()
        assert "deixou de ser alcançável" in body and "Preparar este passo" not in body
    with subtests.test("at the target, no core step and the choice is forgotten"):
        double.status = status(installed="0.5.0", steps=[step("0.6.1")], latest="0.6.1", targets=[])
        body = client.get(url).content.decode()
        assert "Preparar este passo" not in body and "Não há mais atualizações pendentes" in body
        assert updates.TARGET_SESSION_KEY not in client.session
    with subtests.test("afterwards the default path again"):
        body = client.get(url).content.decode()
        assert 'name="version" value="0.6.1"' in body
