import json
import logging

from skeleton.common.logging import SecurityJSONFormatter


def test_log_never_serializes_messages_or_arbitrary_extras():
    record = logging.LogRecord(
        "skeleton.security", logging.WARNING, __file__, 1, "password=never-persist-this", (), None
    )
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


def test_exception_stack_keeps_code_positions_but_not_message_or_source():
    def leaky(token):
        raise ValueError(f"token={token}")

    try:
        leaky("never-persist-this")
    except ValueError:
        import sys

        record = logging.LogRecord(
            "django.request", logging.ERROR, __file__, 1, "boom %s", ("never-persist-this",), sys.exc_info()
        )
    result = SecurityJSONFormatter().format(record)
    data = json.loads(result)
    assert data["error_class"] == "ValueError"
    assert data["error_stack"][-1].startswith("test_security_logging.py:")
    assert data["error_stack"][-1].endswith(":leaky")
    assert "never-persist-this" not in result
    assert "raise ValueError" not in result


def test_exception_stack_is_bounded():
    from skeleton.common.logging import STACK_FRAME_LIMIT

    def recurse(depth):
        if depth == 0:
            raise RuntimeError("deep")
        recurse(depth - 1)

    try:
        recurse(STACK_FRAME_LIMIT * 2)
    except RuntimeError:
        import sys

        record = logging.LogRecord("django.request", logging.ERROR, __file__, 1, "", (), sys.exc_info())
    assert len(json.loads(SecurityJSONFormatter().format(record))["error_stack"]) == STACK_FRAME_LIMIT


def test_unstructured_event_is_replaced():
    record = logging.LogRecord("skeleton.security", logging.INFO, __file__, 1, "", (), None)
    record.event = "reset?token=sensitive"
    assert json.loads(SecurityJSONFormatter().format(record))["event"] == "application_event"
