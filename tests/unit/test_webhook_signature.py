"""Webhook signature vector, retry schedule and response sanitization (#64)."""

from datetime import timedelta

from skeleton.integrations.webhooks.delivery import (
    MAX_CONSECUTIVE_FAILURES,
    retry_delay,
    retryable,
    sanitized_snippet,
    sign,
)

BODY = b'{"id":"evt_1","type":"notes.webhook_ready"}'
CURRENT = "42a3634c4a23b13b6b9711dc81fb1424a540673f5363b94294dc2c2da727f1b1"
PREVIOUS = "721c546bdea12623640144d9f4ee8a7c9e103dcdc2441acd61a3e3b86aab0967"


def test_signature_matches_the_published_vector():
    """HMAC-SHA256 of ``<t>.<body>``; the same vector is in docs/webhooks.md."""
    assert sign(BODY, ["whsec_test_current"], 1700000000) == f"t=1700000000,v1={CURRENT}"


def test_rotation_sends_both_signatures_current_first():
    header = sign(BODY, ["whsec_test_current", "whsec_test_previous"], 1700000000)
    assert header == f"t=1700000000,v1={CURRENT},v1={PREVIOUS}"


def test_retry_schedule_and_retryable_answers():
    expected = [
        timedelta(minutes=1),
        timedelta(minutes=5),
        timedelta(minutes=30),
        timedelta(hours=2),
        timedelta(hours=6),
        timedelta(hours=12),
        timedelta(hours=24),
    ]
    assert [retry_delay(attempt) for attempt in range(1, 8)] == expected
    assert retry_delay(8) is None and retry_delay(0) is None
    assert all(retryable(status) for status in (None, 408, 429, 500, 502, 503, 504))
    assert not any(retryable(status) for status in (400, 401, 403, 404, 410, 422))
    assert MAX_CONSECUTIVE_FAILURES == 15


def test_response_snippet_is_bounded_printable_text():
    assert len(sanitized_snippet(b"x" * 5000)) == 1024
    snippet = sanitized_snippet(b"ok <script>\x00\x1b[31m\xff")
    assert snippet.startswith("ok <script>") and "\x00" not in snippet and "\x1b" not in snippet
