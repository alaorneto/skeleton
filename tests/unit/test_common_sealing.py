"""Canonical bytes and digest of a seal payload (#527): stable, order independent, closed types."""

import datetime as dt
import uuid
from decimal import Decimal

import pytest

from skeleton.common.sealing import canonical_bytes, digest

PAYLOAD = {
    "table": "acme_docs.document",
    "sysid": uuid.UUID("00000000-0000-4000-8000-000000000001"),
    "fields": {"body": "Texto", "amount": Decimal("10.50"), "day": dt.date(2026, 11, 16)},
    "at": dt.datetime(2026, 11, 16, 9, 30, tzinfo=dt.timezone(dt.timedelta(hours=-3))),
    "extra": None,
    "lines": [1, True, "x"],
}


def test_canonical_bytes_are_fixed():
    assert canonical_bytes(PAYLOAD) == (
        b'{"at":"2026-11-16T12:30:00+00:00","extra":null,"fields":{"amount":"10.50","body":"Texto",'
        b'"day":"2026-11-16"},"lines":[1,true,"x"],"sysid":"00000000-0000-4000-8000-000000000001",'
        b'"table":"acme_docs.document"}'
    )


def test_the_digest_is_a_fixed_vector_whatever_the_key_order():
    reordered = dict(reversed(list(PAYLOAD.items())))
    assert digest(PAYLOAD) == digest(reordered)
    assert digest({}) == "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
    assert digest(PAYLOAD) == digest(PAYLOAD) and len(digest(PAYLOAD)) == 64
    assert digest({"a": "ç"}) != digest({"a": "c"})


@pytest.mark.parametrize(
    "value",
    [1.5, dt.datetime(2026, 1, 1), b"bytes", {1: "x"}, object(), {"nested": [0.1]}],
)
def test_floats_naive_datetimes_and_other_types_are_refused(value):
    with pytest.raises(TypeError):
        digest({"value": value})
