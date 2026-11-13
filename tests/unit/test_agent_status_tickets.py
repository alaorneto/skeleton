"""Scoped status tickets and the independent read-only status page of the agent (#247)."""

import hmac
import json
import os
import stat
import time
from base64 import urlsafe_b64encode
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

pytest.importorskip("fcntl", reason="O agente de deployment exige fcntl (Linux ou macOS).")

from deployment_agent import server as server_module  # noqa: E402
from deployment_agent.server import StatusHandler, StatusTickets  # noqa: E402
from deployment_agent.storage import AgentError, Store, canonical  # noqa: E402
from tests.unit.agent_helpers import exchange, exchange_json, http_request  # noqa: E402

pytestmark = pytest.mark.posix
OPERATION = "11111111-2222-4333-8444-555555555555"
OTHER = "66666666-7777-4888-8999-000000000000"
PUBLIC_URL = "https://status.example.test/status"


@pytest.fixture
def agent(tmp_path):
    store = Store(tmp_path / "journal")
    operation = {
        "id": OPERATION,
        "state": "running",
        "stage": "backup_ready",
        "generation": 4,
        "error": None,
        "created_at": "2026-11-13T00:00:00+00:00",
        "updated_at": "2026-11-13T00:01:00+00:00",
        "plan": {"release": "/srv/releases/5-x", "files": [{"path": "config/settings.py"}]},
        "binding": {"actor_id": OTHER, "request_id": OTHER},
        "backup": "/srv/state/backups/x/backup.json",
        "release": "/srv/releases/5-x",
    }
    store.put("operations", OPERATION, operation)
    service = SimpleNamespace(
        store=store,
        config={"status_http": {"port": 8443, "public_url": PUBLIC_URL}},
        operation=lambda operation_id: dict(store.get("operations", operation_id)),
    )
    tickets = StatusTickets(service)
    return SimpleNamespace(
        service=service, tickets=tickets, store=store, server=SimpleNamespace(service=service, tickets=tickets)
    )


def ticket_of(url):
    return parse_qs(urlsplit(url).query)["ticket"][0]


def signed(tickets, body):
    payload = urlsafe_b64encode(canonical(body)).rstrip(b"=")
    signature = urlsafe_b64encode(hmac.digest(tickets.key, payload, "sha256")).rstrip(b"=")
    return (payload + b"." + signature).decode()


def refused(tickets, operation_id, ticket):
    with pytest.raises(AgentError) as raised:
        tickets.verify(operation_id, ticket)
    return (raised.value.code, raised.value.status) == ("invalid_status_ticket", 403)


def test_ticket_issued_for_operation_is_accepted(agent):
    url = agent.tickets.issue(OPERATION)
    assert url.startswith(f"{PUBLIC_URL}/{OPERATION}/?ticket=")
    agent.tickets.verify(OPERATION, ticket_of(url))


def test_ticket_for_another_operation_is_403(agent):
    assert refused(agent.tickets, OTHER, ticket_of(agent.tickets.issue(OPERATION)))


def test_expired_ticket_is_403(agent, monkeypatch):
    ticket = ticket_of(agent.tickets.issue(OPERATION))
    issued = time.time()
    monkeypatch.setattr(server_module.time, "time", lambda: issued + 301)
    assert refused(agent.tickets, OPERATION, ticket)


def test_ticket_with_expiry_beyond_five_minutes_is_403(agent):
    body = {"operation": OPERATION, "expires": int(time.time()) + 3600, "epoch": 0, "nonce": "n"}
    assert refused(agent.tickets, OPERATION, signed(agent.tickets, body))
    body["expires"] = int(time.time()) + 200
    agent.tickets.verify(OPERATION, signed(agent.tickets, body))  # Same key and shape within the window.
    body["expires"] = str(body["expires"])
    assert refused(agent.tickets, OPERATION, signed(agent.tickets, body))


def test_tampered_payload_or_signature_is_403(agent):
    ticket = ticket_of(agent.tickets.issue(OPERATION))
    payload, signature = ticket.split(".")
    forged = {"operation": OTHER, "expires": int(time.time()) + 60, "epoch": 0, "nonce": "n"}
    forged_payload = urlsafe_b64encode(canonical(forged)).rstrip(b"=").decode()
    flipped = signature[:-1] + ("A" if signature[-1] != "A" else "B")
    for value in (f"{forged_payload}.{signature}", f"{payload}.{flipped}", payload, f"{payload}..{signature}", ""):
        assert refused(agent.tickets, OPERATION, value)
    other_key = StatusTickets.__new__(StatusTickets)
    other_key.key = os.urandom(32)
    body = {"operation": OPERATION, "expires": int(time.time()) + 60, "epoch": 0, "nonce": "n"}
    assert refused(agent.tickets, OPERATION, signed(other_key, body))


def test_oversized_ticket_is_403(agent):
    ticket = ticket_of(agent.tickets.issue(OPERATION))
    assert refused(agent.tickets, OPERATION, ticket + "A" * (1025 - len(ticket)))


def test_revoke_invalidates_previously_issued_tickets(agent):
    ticket = ticket_of(agent.tickets.issue(OPERATION))
    agent.tickets.revoke()
    assert refused(agent.tickets, OPERATION, ticket)
    agent.tickets.verify(OPERATION, ticket_of(agent.tickets.issue(OPERATION)))  # New tickets carry the new epoch.


def test_signing_key_file_is_private(agent):
    key = agent.store.root / "status-signing.key"
    assert stat.S_IMODE(key.stat().st_mode) & 0o077 == 0 and len(key.read_bytes()) == 32
    assert StatusTickets(agent.service).key == agent.tickets.key  # Reused, never regenerated.
    os.chmod(key, 0o644)
    with pytest.raises(AgentError, match="insecure_private_file"):
        StatusTickets(agent.service)


def status_get(agent, path):
    return exchange_json(StatusHandler, agent.server, http_request("GET", path, token=None))


def test_status_page_exposes_only_summary_keys(agent):
    ticket = ticket_of(agent.tickets.issue(OPERATION))
    status, body = status_get(agent, f"/status/{OPERATION}/?ticket={ticket}&format=json")
    assert status == 200
    assert set(body) == {"id", "state", "stage", "generation", "error", "created_at", "updated_at"}
    assert body["stage"] == "backup_ready" and body["generation"] == 4
    serialized = json.dumps(body)
    assert "/srv/" not in serialized and OTHER not in serialized


def test_status_page_before_submission_reveals_nothing_but_waiting(agent):
    ticket = ticket_of(agent.tickets.issue(OTHER))
    status, body = status_get(agent, f"/status/{OTHER}/?ticket={ticket}&format=json")
    assert status == 200 and body["state"] == "awaiting_submission" and body["generation"] is None


def test_status_page_escapes_values(agent):
    operation = agent.store.get("operations", OPERATION)
    agent.store.put("operations", OPERATION, {**operation, "error": '<script>alert("x")</script>'})
    ticket = ticket_of(agent.tickets.issue(OPERATION))
    status, headers, body = exchange(
        StatusHandler, agent.server, http_request("GET", f"/status/{OPERATION}/?ticket={ticket}", token=None)
    )
    page = body.decode()
    assert status == 200 and headers["Content-Type"].startswith("text/html")
    assert "<script>" not in page and "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;" in page
    assert "default-src 'none'" in headers["Content-Security-Policy"]
    assert headers["Cache-Control"] == "no-store" and headers["Referrer-Policy"] == "no-referrer"


def test_status_page_refuses_missing_extra_or_repeated_ticket(agent):
    ticket = ticket_of(agent.tickets.issue(OPERATION))
    for query in ("", f"ticket={ticket}&debug=1", f"ticket={ticket}&ticket={ticket}", "ticket=forged"):
        assert status_get(agent, f"/status/{OPERATION}/?{query}") == (403, {"error": "invalid_status_ticket"})


def test_status_path_outside_prefix_is_404(agent):
    ticket = ticket_of(agent.tickets.issue(OPERATION))
    for path in (f"/{OPERATION}/?ticket={ticket}", f"/statusx/{OPERATION}/?ticket={ticket}", "/status"):
        assert status_get(agent, path) == (404, {"error": "not_found"})
    assert status_get(agent, f"/status/not-a-uuid/?ticket={ticket}") == (400, {"error": "invalid_uuid"})


def test_no_status_url_without_status_http_configuration(agent):
    agent.service.config = {}
    assert agent.tickets.issue(OPERATION) is None
