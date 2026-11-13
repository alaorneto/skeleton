"""Provider HTTP seam, outbound size bound and the public URL warning (#400). No network."""

import pytest

from skeleton.billing.providers import http
from skeleton.billing.providers.base import ProviderError, invoice_reference, parse_invoice_reference
from skeleton.common import outbound_http
from skeleton.common.outbound_http import OutboundResponse


@pytest.fixture
def recorded(monkeypatch):
    sent = []
    answers = []

    def transport(method, url, *, headers, body):
        sent.append((method, url, dict(headers), body))
        return answers.pop(0)

    monkeypatch.setattr(http, "transport", transport)
    return sent, answers


def test_call_encodes_json_and_forms_and_decodes_the_answer(recorded):
    sent, answers = recorded
    answers += [OutboundResponse(200, {}, b'{"id": "x"}', False), OutboundResponse(201, {}, b"", False)]
    assert http.call("POST", "https://api.example.test/a", headers={"Authorization": "k"}, json_body={"a": 1}) == (
        200,
        {"id": "x"},
    )
    assert http.call("POST", "https://api.example.test/b", headers={}, form=[("x", "1"), ("y", "é")]) == (201, None)
    assert sent[0][2]["Content-Type"] == "application/json" and sent[0][3] == b'{"a":1}'
    assert sent[1][2]["Content-Type"] == "application/x-www-form-urlencoded" and sent[1][3] == b"x=1&y=%C3%A9"


def test_server_errors_are_retryable_and_truncation_is_not(recorded, subtests):
    _, answers = recorded
    for status, retryable in ((500, True), (503, True), (429, True)):
        answers.append(OutboundResponse(status, {}, b"{}", False))
        with subtests.test(status=status), pytest.raises(ProviderError) as error:
            http.call("GET", "https://api.example.test/", headers={})
        assert error.value.retryable is retryable
    answers.append(OutboundResponse(200, {}, b"{}", True))
    with pytest.raises(ProviderError) as truncated:
        http.call("GET", "https://api.example.test/", headers={})
    assert (truncated.value.code, truncated.value.retryable) == ("response_too_large", False)


def test_network_failures_map_to_provider_errors(monkeypatch):
    def failing(method, url, *, headers, body):
        raise outbound_http.OutboundRequestFailed("down")

    def refused(method, url, *, headers, body):
        raise outbound_http.OutboundRequestRefused("private_address", "no")

    monkeypatch.setattr(http, "transport", failing)
    with pytest.raises(ProviderError) as failed:
        http.call("GET", "https://api.example.test/", headers={})
    assert failed.value.retryable
    monkeypatch.setattr(http, "transport", refused)
    with pytest.raises(ProviderError) as blocked:
        http.call("GET", "https://api.example.test/", headers={})
    assert (blocked.value.code, blocked.value.retryable) == ("refused:private_address", False)


def test_outbound_response_bound_is_validated():
    for value in (0, outbound_http.MAX_RESPONSE_BYTES_CEILING + 1):
        with pytest.raises(ValueError):
            outbound_http.request("GET", "https://example.test/", max_response_bytes=value)


def test_invoice_reference_round_trip():
    from uuid import uuid4

    organization, invoice = uuid4(), uuid4()
    assert parse_invoice_reference(invoice_reference(organization, invoice)) == (organization, invoice)
    assert parse_invoice_reference("garbage") is None
    assert parse_invoice_reference(f"{organization}:not-a-uuid") is None


def test_public_url_warning_only_with_billing_on(settings):
    from skeleton.billing.checks import billing_public_url_check

    settings.SKELETON_BILLING_ENABLED = False
    settings.SKELETON_PUBLIC_URL = ""
    assert billing_public_url_check() == []
    settings.SKELETON_BILLING_ENABLED = True
    [warning] = billing_public_url_check()
    assert warning.id == "skeleton.W037"
    settings.SKELETON_PUBLIC_URL = "https://app.example.test/path"
    assert len(billing_public_url_check()) == 1
    settings.SKELETON_PUBLIC_URL = "https://app.example.test"
    assert billing_public_url_check() == []
