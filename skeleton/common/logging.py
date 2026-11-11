import json
import logging
import traceback
from datetime import UTC, datetime
from pathlib import PurePath
from uuid import UUID

STACK_FRAME_LIMIT = 25


def sanitized_stack(tb, *, limit=STACK_FRAME_LIMIT):
    """Locate a failure by code position only: file name, line and function, innermost last.

    Exception messages, source lines and local values are deliberately excluded because they
    may carry request data or secrets.
    """
    frames = traceback.extract_tb(tb)[-limit:]
    return [frame_location(frame.filename, frame.lineno, frame.name) for frame in frames]


def frame_location(filename, lineno, function):
    """``file:line:function`` with the bare file name: no directory (it may name a user or host)."""
    return f"{safe_file_name(filename)}:{lineno}:{function}"


def safe_file_name(filename):
    return PurePath(str(filename or "")).name


def safe_event_name(event):
    """An event name made of letters, digits, ``_`` and ``.``; anything else is replaced."""
    if not isinstance(event, str) or not event.replace("_", "").replace(".", "").isalnum():
        return "application_event"
    return event


def safe_fields(fields):
    """The structured values that may leave the process: logs and Sentry share this allowlist.

    Only the enumerated keys survive, and only as short scalars; ``target_sysid`` must be a
    UUID and ``error_class`` an identifier. Messages and arbitrary extras never pass.
    """
    allowed = {}
    for key in SecurityJSONFormatter.extra_fields:
        value = fields.get(key)
        if value is None or not isinstance(value, (str, int, bool)):
            continue
        if key == "target_sysid":
            try:
                value = str(UUID(str(value)))
            except (ValueError, TypeError, AttributeError):
                continue
        if key == "error_class" and (not isinstance(value, str) or not value.isidentifier()):
            continue
        allowed[key] = value[:128] if isinstance(value, str) else value
    return allowed


class SecurityJSONFormatter(logging.Formatter):
    """Only enumerated structured values reach logs; messages/tracebacks may contain secrets."""

    extra_fields = frozenset(
        {
            "status",
            "count",
            "operation",
            "model_label",
            "record_sysid",
            "reason_code",
            "error_class",
            "outcome",
            "target_sysid",
        }
    )

    def format(self, record):
        from skeleton.tenancy.context import current_context

        context = getattr(record, "security_context", None) or current_context()
        event = safe_event_name(getattr(record, "event", "application_event"))
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "event": event,
            "request_id": str(context.request_id) if context.request_id else getattr(record, "request_id", None),
            "user_sysid": str(context.actor) if context.actor else None,
            "organization_sysid": str(context.organization_id) if context.organization_id else None,
            "source": context.source if context.request_id else (getattr(record, "source", None) or context.source),
            "status": getattr(record, "status", getattr(record, "status_code", None)),
            "logger": record.name,
        }
        if record.exc_info and record.exc_info[0]:
            payload["error_class"] = record.exc_info[0].__name__
            payload["error_stack"] = sanitized_stack(record.exc_info[2])
        payload.update(safe_fields({key: getattr(record, key, None) for key in self.extra_fields}))
        return json.dumps(payload, ensure_ascii=False)


def security_event(
    event, *, level=logging.INFO, context_snapshot=None, request_id=None, source=None, exc_info=None, **fields
):
    allowed = {key: value for key, value in fields.items() if key in SecurityJSONFormatter.extra_fields}
    logging.getLogger("skeleton.security").log(
        level,
        "",
        exc_info=exc_info,
        extra={
            "event": event,
            "security_context": context_snapshot,
            "request_id": request_id,
            "source": source,
            **allowed,
        },
    )
