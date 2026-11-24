"""/platform/updates/ in a cluster of agents (#684): nodes, the primary and the rolling restart."""

from types import SimpleNamespace

import pytest
from django.test import Client
from django.urls import reverse

from skeleton.accounts.services import bootstrap_identity
from skeleton.system.deployment_client import AgentError, sanitize_cluster
from skeleton.web.platform import updates

pytestmark = pytest.mark.usefixtures("skeleton_db")
PASSWORD = "Cluster-updates-Password-2026!"

CLUSTER = {
    "mode": "cluster",
    "node_id": "node-b",
    "role": "follower",
    "is_primary": False,
    "primary": {"node_id": "node-a", "epoch": 4, "renewed_age_s": 2.0, "cluster_url": "https://secret.internal"},
    "nodes": [
        {"node_id": "node-a", "role": "candidate", "release": "7-plan", "alive": True, "heartbeat_age_s": 1.0},
        {
            "node_id": "node-b",
            "role": "follower",
            "release": "7-plan",
            "alive": False,
            "heartbeat_age_s": 90.0,
            "token": "never-shown",
        },
    ],
    "problem": None,
}


@pytest.fixture
def admin():
    user, _ = bootstrap_identity(
        email="cluster-updates@example.test", organization="cluster-updates", name="Platform", password=PASSWORD
    )
    client = Client()
    client.get(reverse("account_login"))
    response = client.post(
        reverse("account_login"),
        {"login": user.email, "password": PASSWORD, "csrfmiddlewaretoken": client.cookies["csrftoken"].value},
    )
    assert response.status_code == 302
    return client


def agent(monkeypatch, cluster=CLUSTER, restart=None):
    calls = []
    double = SimpleNamespace(
        inventory=lambda: {"generation": 7, "apps": [], "core": {}, "agent": {"version": "0.4.0"}},
        cluster=lambda: sanitize_cluster(cluster),
        cluster_restart=restart or (lambda: calls.append("restart") or {"state": "started"}),
    )
    monkeypatch.setattr(updates, "agent_client", lambda: double)
    return calls


def test_the_cluster_panel_shows_the_primary_and_the_nodes_without_internal_fields(admin, monkeypatch):
    agent(monkeypatch)
    html = admin.get(reverse("platform:updates")).content.decode()
    assert "Nós do cluster" in html and "node-a" in html and "node-b" in html and "época 4" in html
    assert "Sem sinal" in html  # node-b has no recent heartbeat.
    assert "secret.internal" not in html and "never-shown" not in html


def test_a_single_node_shows_no_panel(admin, monkeypatch):
    agent(monkeypatch, cluster={"mode": "single"})
    assert "Nós do cluster" not in admin.get(reverse("platform:updates")).content.decode()


def test_without_a_primary_the_page_says_so_and_disables_the_restart(admin, monkeypatch):
    agent(monkeypatch, cluster={**CLUSTER, "primary": None})
    html = admin.get(reverse("platform:updates")).content.decode()
    assert "Nenhum agente primário ativo" in html and "disabled" in html


def test_the_rolling_restart_needs_csrf_and_reaches_the_agent(admin, monkeypatch):
    calls = agent(monkeypatch)
    strict = Client(enforce_csrf_checks=True)
    strict.cookies = admin.cookies
    assert strict.post(reverse("platform:updates-cluster-restart")).status_code == 403
    response = admin.post(reverse("platform:updates-cluster-restart"))
    assert response.status_code == 302 and calls == ["restart"]

    def busy():
        raise AgentError("cluster_busy", 409)

    agent(monkeypatch, restart=busy)
    page = admin.post(reverse("platform:updates-cluster-restart"), follow=True).content.decode()
    assert "operação em curso no cluster" in page


def test_sanitize_keeps_a_closed_shape():
    assert sanitize_cluster({"mode": "anything"}) == {"mode": "single"}
    clean = sanitize_cluster(CLUSTER)
    assert set(clean["primary"]) == {"node_id", "epoch", "renewed_age_s"}
    assert all("token" not in node for node in clean["nodes"])


def test_the_panel_shows_each_agent_version_and_warns_when_alive_nodes_differ(admin, monkeypatch):
    a, b = ({**node, "alive": True} for node in CLUSTER["nodes"])
    same = {**CLUSTER, "nodes": [{**a, "agent_version": "0.7.0"}, {**b, "agent_version": "0.7.0"}]}
    agent(monkeypatch, cluster=same)
    html = admin.get(reverse("platform:updates")).content.decode()
    assert '<th scope="col">Agente</th>' in html and "0.7.0" in html and "versões diferentes" not in html
    # A node without a heartbeat does not count: it is not part of the next operation.
    gone = {**same, "nodes": [same["nodes"][0], {**b, "agent_version": "0.6.0", "alive": False}]}
    assert sanitize_cluster(gone)["agent_versions_differ"] is False
    agent(monkeypatch, cluster={**same, "nodes": [same["nodes"][0], {**b, "agent_version": "0.6.0"}]})
    html = admin.get(reverse("platform:updates")).content.decode()
    assert "Os agentes dos nós estão em versões diferentes" in html and "0.6.0" in html


def test_a_problem_of_the_node_reads_as_a_sentence_never_as_the_bare_code(admin, monkeypatch):
    agent(monkeypatch, cluster={**CLUSTER, "problem": "agent_update_manual"})
    html = admin.get(reverse("platform:updates")).content.decode()
    assert "atualize a imagem dele" in html and "agent_update_manual" not in html
    agent(monkeypatch, cluster={**CLUSTER, "problem": "something_new"})
    html = admin.get(reverse("platform:updates")).content.decode()
    assert "informou um problema (something_new)" in html
