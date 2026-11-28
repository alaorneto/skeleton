"""Proxy-aware client IP resolution helper (#862)."""

from django.test import RequestFactory, override_settings

from skeleton.common.client_ip import client_ip


def test_without_trusted_proxy_forwarded_headers_are_ignored():
    factory = RequestFactory()
    with override_settings(ALLAUTH_TRUSTED_PROXY_COUNT=0, ALLAUTH_TRUSTED_CLIENT_IP_HEADER=None):
        request = factory.get(
            "/",
            REMOTE_ADDR="10.0.0.1",
            HTTP_X_FORWARDED_FOR="203.0.113.10",
            HTTP_X_REAL_IP="203.0.113.20",
        )
        assert client_ip(request) == "10.0.0.1"


def test_trusted_proxy_count_reads_rightmost_untrusted_hop():
    factory = RequestFactory()
    with override_settings(ALLAUTH_TRUSTED_PROXY_COUNT=1, ALLAUTH_TRUSTED_CLIENT_IP_HEADER=None):
        request = factory.get(
            "/",
            REMOTE_ADDR="10.0.0.1",
            HTTP_X_FORWARDED_FOR="198.51.100.5, 203.0.113.10",
        )
        assert client_ip(request) == "203.0.113.10"


def test_trusted_client_ip_header_reads_configured_header():
    factory = RequestFactory()
    with override_settings(ALLAUTH_TRUSTED_PROXY_COUNT=0, ALLAUTH_TRUSTED_CLIENT_IP_HEADER="X-Real-IP"):
        request = factory.get(
            "/",
            REMOTE_ADDR="10.0.0.1",
            HTTP_X_FORWARDED_FOR="198.51.100.5",
            HTTP_X_REAL_IP="203.0.113.42",
        )
        assert client_ip(request) == "203.0.113.42"
