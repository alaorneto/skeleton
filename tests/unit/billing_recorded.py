"""Recorded provider responses for the adapter tests (#401–#403): no network."""

import json
from dataclasses import dataclass, field

import pytest

from skeleton.billing.providers import http
from skeleton.common.outbound_http import OutboundResponse


@dataclass
class Account:
    """What an adapter reads from a ProviderAccount."""

    api_key: str = "sk_test_key"
    webhook_secret: str = "whsec_test_vector"
    mode: str = "test"

    def secret(self, name):
        return getattr(self, name)


@dataclass
class Recorder:
    """Answers each request with the next recorded response whose method and URL prefix match."""

    answers: list = field(default_factory=list)
    sent: list = field(default_factory=list)

    def add(self, method, prefix, status, data):
        self.answers.append((method, prefix, status, data))

    def __call__(self, method, url, *, headers, body):
        self.sent.append((method, url, dict(headers), body))
        for index, (want, prefix, status, data) in enumerate(self.answers):
            if want == method and url.startswith(prefix):
                del self.answers[index]
                return OutboundResponse(status, {}, json.dumps(data).encode(), False)
        raise AssertionError(f"Unexpected request {method} {url}")

    def form(self, index=-1):
        from urllib.parse import parse_qsl

        return dict(parse_qsl(self.sent[index][3].decode()))

    def json(self, index=-1):
        return json.loads(self.sent[index][3])


@pytest.fixture
def recorder(monkeypatch):
    recorded = Recorder()
    monkeypatch.setattr(http, "transport", recorded)
    return recorded
