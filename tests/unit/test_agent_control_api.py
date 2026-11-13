"""Control channel of the deployment agent: bearer token, body limits and closed fields (#247).

The real ControlHandler serves each request over a socket pair, so the request line, headers
and body go through http.server exactly as on the Unix socket.
"""

import os
from types import SimpleNamespace

import pytest

pytest.importorskip("fcntl", reason="O agente de deployment exige fcntl (Linux ou macOS).")

from deployment_agent.server import ControlHandler  # noqa: E402
from deployment_agent.storage import AgentError  # noqa: E402
from tests.unit.agent_helpers import (  # noqa: E402
    TOKEN,
    exchange,
    exchange_json,
    http_request,
    json_request,
    write_private,
)

pytestmark = pytest.mark.posix
OPERATION = "11111111-2222-4333-8444-555555555555"


@pytest.fixture
def agent(tmp_path):
    """A server object with a service double that records every call reaching it."""
    calls = []

    def recorded(name, result=None):
        def method(*args, **kwargs):
            calls.append(name)
            return result if result is not None else {"called": name}

        return method

    service = SimpleNamespace(
        config={"token_file": write_private(tmp_path / "control-token", TOKEN), "max_upload_bytes": 1024},
        inventory=recorded("inventory", {"generation": 0}),
        preview=recorded("preview"),
        submit=recorded("submit", {"id": OPERATION}),
        check_updates=recorded("check_updates"),
        fetch_release=recorded("fetch_release"),
        upload=recorded("upload"),
        operation=recorded("operation", {"id": OPERATION}),
        plan_status=recorded("plan_status"),
    )
    tickets = SimpleNamespace(issue=lambda operation_id: None)
    return SimpleNamespace(server=SimpleNamespace(service=service, tickets=tickets), calls=calls, tmp_path=tmp_path)


def send(agent, request):
    return exchange_json(ControlHandler, agent.server, request)


def test_valid_token_reaches_the_service(agent):
    assert send(agent, http_request("GET", "/inventory")) == (200, {"generation": 0})
    assert agent.calls == ["inventory"]


def test_request_without_authorization_is_401(agent):
    assert send(agent, http_request("GET", "/inventory", token=None)) == (401, {"error": "unauthorized"})
    assert agent.calls == []


def test_wrong_bearer_token_is_401(agent):
    for token in ("wrong", TOKEN[:-1], TOKEN + "x", TOKEN.upper()):
        assert send(agent, http_request("GET", "/inventory", token=token)) == (401, {"error": "unauthorized"})
    request = http_request("GET", "/inventory", token=None, headers={"Authorization": f"Basic {TOKEN}"})
    assert send(agent, request) == (401, {"error": "unauthorized"})
    assert agent.calls == []


def test_short_token_file_refuses_every_request(agent):
    short = "s" * 10
    write_private(agent.tmp_path / "control-token", short)
    assert send(agent, http_request("GET", "/inventory", token=short)) == (401, {"error": "unauthorized"})
    assert send(agent, json_request("/preview", {"action": "install"}, token=short))[0] == 401
    assert agent.calls == []


def test_token_file_readable_by_others_refuses_every_request(agent):
    os.chmod(agent.server.service.config["token_file"], 0o644)
    for request in (http_request("GET", "/inventory"), json_request("/preview", {"action": "install"})):
        assert send(agent, request) == (400, {"error": "insecure_private_file"})
    assert agent.calls == []


def test_body_is_not_read_before_authentication(agent):
    request = http_request(
        "POST", "/preview", token="wrong", headers={"Content-Length": "999999999", "Content-Type": "application/json"}
    )
    assert send(agent, request) == (401, {"error": "unauthorized"})


def test_json_body_over_one_megabyte_is_413(agent):
    request = http_request(
        "POST", "/preview", headers={"Content-Length": str(1024 * 1024 + 1), "Content-Type": "application/json"}
    )
    assert send(agent, request) == (413, {"error": "request_too_large"})
    assert agent.calls == []


def test_upload_over_configured_limit_is_413(agent):
    request = http_request(
        "POST", "/uploads", headers={"Content-Length": "1025", "Content-Type": "application/octet-stream"}
    )
    assert send(agent, request) == (413, {"error": "request_too_large"})


def test_missing_content_length_is_411(agent):
    request = http_request("POST", "/preview", body=b"{}", headers={"Content-Type": "application/json"}, length=False)
    assert send(agent, request) == (411, {"error": "content_length_required"})
    assert agent.calls == []


def test_transfer_encoding_is_refused(agent):
    request = http_request(
        "POST",
        "/preview",
        body=b"2\r\n{}\r\n0\r\n\r\n",
        headers={"Transfer-Encoding": "chunked", "Content-Type": "application/json"},
        length=False,
    )
    assert send(agent, request) == (400, {"error": "unsupported_transfer_encoding"})
    assert agent.calls == []


@pytest.mark.parametrize(
    "path, content_type, body",
    [
        ("/preview", "text/plain", b'{"action": "install"}'),
        ("/preview", "application/octet-stream", b'{"action": "install"}'),
        ("/uploads", "application/json", b"PK"),
    ],
)
def test_wrong_content_type_is_415(agent, path, content_type, body):
    request = http_request("POST", path, body=body, headers={"Content-Type": content_type})
    assert send(agent, request) == (415, {"error": "invalid_content_type"})
    assert agent.calls == []


@pytest.mark.parametrize("body", [b"[]", b'"text"', b"1", b"null", b"not json"])
def test_non_object_json_is_refused(agent, body):
    request = http_request("POST", "/preview", body=body, headers={"Content-Type": "application/json"})
    assert send(agent, request) == (400, {"error": "invalid_json"})
    assert agent.calls == []


@pytest.mark.parametrize(
    "path, body, code",
    [
        ("/preview", {"action": "install", "artifacts": [], "app_ids": [], "force": True}, "unknown_fields"),
        (
            "/submit",
            {
                "plan_id": OPERATION,
                "plan_digest": "a" * 64,
                "generation": 0,
                "operation_id": OPERATION,
                "actor_id": OPERATION,
                "request_id": OPERATION,
                "state": "succeeded",
            },
            "invalid_submit_fields",
        ),
        ("/status-ticket", {"operation_id": OPERATION, "expires": 9999999999}, "invalid_status_ticket_fields"),
        ("/catalog/check", {"source_id": "main"}, "unknown_fields"),
        (
            "/catalog/fetch",
            {"component_id": "notes", "version": "1.0.0", "source_id": "main", "url": "https://x"},
            "invalid_catalogue_selection",
        ),
    ],
)
def test_unknown_fields_are_refused(agent, path, body, code):
    assert send(agent, json_request(path, body)) == (400, {"error": code})
    assert agent.calls == []


def test_query_string_on_control_path_is_refused(agent):
    assert send(agent, http_request("GET", "/inventory?debug=1")) == (400, {"error": "invalid_control_path"})
    assert send(agent, http_request("GET", "/inventory#x")) == (400, {"error": "invalid_control_path"})
    assert agent.calls == []


def test_unknown_path_is_404(agent):
    assert send(agent, http_request("GET", "/journal")) == (404, {"error": "not_found"})
    assert send(agent, json_request("/recover", {})) == (404, {"error": "not_found"})
    assert exchange(ControlHandler, agent.server, http_request("DELETE", "/inventory"))[0] == 501  # No do_DELETE.
    assert agent.calls == []


def test_malformed_operation_identifier_is_refused(agent):
    assert send(agent, http_request("GET", "/operations/../active")) == (400, {"error": "invalid_uuid"})
    assert send(agent, http_request("GET", "/plans/not-a-uuid")) == (400, {"error": "invalid_uuid"})
    assert agent.calls == []


def test_error_response_carries_only_code_and_details(agent):
    details = [{"code": "missing_dependency", "component": "a", "dependency": "b"}]

    def refused(*args, **kwargs):
        raise AgentError("dependency_unsatisfied", 409, details)

    agent.server.service.preview = refused
    body = {"action": "install", "artifacts": [], "app_ids": []}
    assert send(agent, json_request("/preview", body)) == (409, {"error": "dependency_unsatisfied", "details": details})

    def plain(*args, **kwargs):
        raise AgentError("deployment_busy", 409)

    agent.server.service.preview = plain
    assert send(agent, json_request("/preview", body)) == (409, {"error": "deployment_busy"})
