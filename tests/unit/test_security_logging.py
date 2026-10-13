import json
import logging

from skeleton.common.logging import SecurityJSONFormatter


def test_log_never_serializes_messages_or_arbitrary_extras():
    record = logging.LogRecord("skeleton.security", logging.WARNING, __file__, 1, "password=never-persist-this", (), None)
    record.event = "access.denied"
    record.status = 403
    record.password = "never-persist-this"
    record.token = "never-persist-this"
    record.body = {"secret": "never-persist-this"}
    result = SecurityJSONFormatter().format(record)
    data = json.loads(result)
    assert data["event"] == "access.denied"
    assert data["status"] == 403
    assert "never-persist-this" not in result
    assert "body" not in data


def test_unstructured_event_is_replaced():
    record = logging.LogRecord("skeleton.security", logging.INFO, __file__, 1, "", (), None)
    record.event = "reset?token=sensitive"
    assert json.loads(SecurityJSONFormatter().format(record))["event"] == "application_event"
