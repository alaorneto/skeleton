"""Deadlines of the deployment agent client (#250): short for status reads and submissions."""

import pytest

from skeleton.system import deployment_client
from skeleton.system.deployment_client import AgentClient


@pytest.fixture
def opened(monkeypatch, tmp_path):
    """A fake connection that records the deadline of each call and answers {}."""
    deadlines = []

    class Response:
        status = 200

        def read(self, size):
            return b"{}"

    class Connection:
        def __init__(self, path, timeout):
            deadlines.append(timeout)

        def request(self, *args, **kwargs):
            pass

        def getresponse(self):
            return Response()

        def close(self):
            pass

    monkeypatch.setattr(deployment_client, "UnixConnection", Connection)
    token = tmp_path / "agent.token"
    token.write_text("t" * 40)
    socket = tmp_path / "agent.sock"
    return deadlines, str(socket), str(token)


OPERATION = "00000000-0000-4000-8000-000000000000"


def test_status_calls_use_short_timeout(opened):
    deadlines, socket, token = opened
    client = AgentClient(socket, token)
    client.inventory()
    client.operation(OPERATION)
    client.status_ticket(OPERATION)
    client.submit(OPERATION, "a" * 64, 1, OPERATION, OPERATION, OPERATION)
    assert deadlines == [10, 10, 10, 30]


def test_long_calls_keep_instance_timeout(opened):
    deadlines, socket, token = opened
    client = AgentClient(socket, token)
    client.upload(b"artifact")
    client.preview("install")
    client.check_updates()
    client.fetch_release("component", "1.0.0", "source")
    assert deadlines == [1800, 1800, 1800, 1800]


def test_instance_timeout_caps_status_timeout(opened):
    deadlines, socket, token = opened
    client = AgentClient(socket, token, timeout=5)
    client.inventory()
    client.submit(OPERATION, "a" * 64, 1, OPERATION, OPERATION, OPERATION)
    client.preview("install")
    assert deadlines == [5, 5, 5]


def test_update_status_is_a_short_get(opened, monkeypatch):
    _, socket, token = opened
    client = AgentClient(socket, token)
    calls = []

    def request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {}

    monkeypatch.setattr(client, "_request", request)
    client.update_status()
    assert calls == [("GET", "/updates/status", {"timeout": deployment_client.STATUS_TIMEOUT})]


def test_update_agent_waits_like_a_submission(opened, monkeypatch):
    _, socket, token = opened
    client = AgentClient(socket, token)
    calls = []

    def request(method, path, body=None, **kwargs):
        calls.append((method, path, body, kwargs))
        return {}

    monkeypatch.setattr(client, "_request", request)
    client.update_agent("a" * 64, actor_id=OPERATION)
    assert calls == [
        (
            "POST",
            "/agent/update",
            {"release": "a" * 64, "actor_id": OPERATION},
            {"timeout": deployment_client.SUBMIT_TIMEOUT},
        )
    ]
