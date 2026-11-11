"""Sentry error reports (#177), with personal data and secrets removed before sending.

Sentry starts only when ``SKELETON_SENTRY_DSN`` (a secret of the instance catalog) is set and the
``[sentry]`` extra is installed; otherwise ``sentry_sdk`` is never imported. ``send_default_pii``
stays off, request bodies are never read, local variables and source lines are not collected,
and performance monitoring is off (tracing is OpenTelemetry's job, #176).

:func:`before_send` is the last step and works by allowlist, like the JSON logs: it rebuilds the
event from the exception types and code positions, the log event name, the safe structured
fields of :func:`skeleton.common.logging.safe_fields`, the HTTP method and the ``request_id``.
Exception messages, URLs, query strings, headers, cookies, bodies, user data, local variables,
breadcrumb messages and arbitrary extras never leave the process.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from django.conf import settings

from skeleton.common.http_logging import current_request_id
from skeleton.common.logging import STACK_FRAME_LIMIT, safe_event_name, safe_fields, safe_file_name

from . import SENTRY_DISTRIBUTIONS, installed

REQUEST_ID_TAG = "skeleton.request_id"
EVENT_TAG = "skeleton.event"
# Event keys copied as they are: SDK and operator metadata, never request or user data.
KEPT_KEYS = ("event_id", "timestamp", "level", "platform", "logger", "environment", "release", "dist", "server_name")
HTTP_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})
CHAIN_SOURCES = frozenset({"__context__", "__cause__"})
_DOTTED = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{0,199}")
_FUNCTION = re.compile(r"[A-Za-z_<][A-Za-z0-9_.<>]{0,199}")
_HEX = re.compile(r"[0-9a-f]{16,32}")
_CATEGORY = re.compile(r"[a-z][a-z0-9_.]{0,63}")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)

_configured = False


def sentry_dsn() -> str:
    return getattr(settings, "SKELETON_SENTRY_DSN", "") or ""


def sentry_enabled() -> bool:
    return bool(sentry_dsn()) and installed(SENTRY_DISTRIBUTIONS)


def _uuid(value: Any) -> str | None:
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _dotted(value: Any) -> str | None:
    return value if isinstance(value, str) and _DOTTED.fullmatch(value) else None


def safe_frame(frame: Mapping[str, Any]) -> dict[str, Any]:
    """The code position only: bare file name, line, function and module, as in ``error_stack``."""
    clean: dict[str, Any] = {"filename": safe_file_name(frame.get("filename") or frame.get("abs_path"))}
    if isinstance(frame.get("lineno"), int):
        clean["lineno"] = frame["lineno"]
    function = frame.get("function")
    if isinstance(function, str) and _FUNCTION.fullmatch(function):
        clean["function"] = function
    if module := _dotted(frame.get("module")):
        clean["module"] = module
    if isinstance(frame.get("in_app"), bool):
        clean["in_app"] = frame["in_app"]
    return clean


def _safe_exception(value: Mapping[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {"type": _dotted(value.get("type")) or "Exception"}
    if module := _dotted(value.get("module")):
        clean["module"] = module
    mechanism = value.get("mechanism")
    if isinstance(mechanism, Mapping):
        kept: dict[str, Any] = {}
        if kind := _dotted(mechanism.get("type")):
            kept["type"] = kind
        for key in ("handled", "is_exception_group"):
            if isinstance(mechanism.get(key), bool):
                kept[key] = mechanism[key]
        for key in ("exception_id", "parent_id"):
            if isinstance(mechanism.get(key), int) and not isinstance(mechanism.get(key), bool):
                kept[key] = mechanism[key]
        if mechanism.get("source") in CHAIN_SOURCES:
            kept["source"] = mechanism["source"]
        clean["mechanism"] = kept
    frames = (value.get("stacktrace") or {}).get("frames") or []
    if isinstance(frames, list):
        clean["stacktrace"] = {
            "frames": [safe_frame(frame) for frame in frames[-STACK_FRAME_LIMIT:] if isinstance(frame, Mapping)]
        }
    return clean


def safe_breadcrumb(crumb: Mapping[str, Any]) -> dict[str, Any]:
    """Type, category, level and time: never the message (SQL, log text) or the data (params, URLs)."""
    clean: dict[str, Any] = {}
    for key in ("type", "category", "level"):
        value = crumb.get(key)
        if isinstance(value, str) and _CATEGORY.fullmatch(value):
            clean[key] = value
    if isinstance(crumb.get("timestamp"), (str, int, float)) or hasattr(crumb.get("timestamp"), "isoformat"):
        clean["timestamp"] = crumb["timestamp"]
    return clean


def _safe_transaction(value: Any) -> str | None:
    """A route pattern (``/o/<uuid:organization_id>/``) passes; anything with a value in it does not."""
    if not isinstance(value, str) or len(value) > 200 or "@" in value or "?" in value or _UUID.search(value):
        return None
    return value


def redact_event(event: Mapping[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {key: event[key] for key in KEPT_KEYS if key in event}
    if isinstance(event.get("sdk"), Mapping):
        sdk = event["sdk"]
        clean["sdk"] = {key: sdk[key] for key in ("name", "version") if isinstance(sdk.get(key), str)}
    if transaction := _safe_transaction(event.get("transaction")):
        clean["transaction"] = transaction
    values = (event.get("exception") or {}).get("values") or []
    if isinstance(values, list) and values:
        clean["exception"] = {"values": [_safe_exception(value) for value in values if isinstance(value, Mapping)]}
    extra = _mapping(event.get("extra"))
    tags: dict[str, str] = {}
    if "logentry" in event or "message" in event:
        name = safe_event_name(extra.get("event"))
        clean["logentry"] = {"message": name}
        tags[EVENT_TAG] = name
    fields = safe_fields(extra)
    if fields:
        clean["extra"] = fields
    request_id = _uuid(current_request_id()) or _uuid(extra.get("request_id"))
    if request_id:
        tags[REQUEST_ID_TAG] = request_id
    if tags:
        clean["tags"] = tags
    request = event.get("request")
    if isinstance(request, Mapping) and request.get("method") in HTTP_METHODS:
        clean["request"] = {"method": request["method"]}
    contexts = _mapping(event.get("contexts"))
    kept_contexts: dict[str, Any] = {}
    trace = contexts.get("trace")
    if isinstance(trace, Mapping):
        ids = {
            key: trace[key]
            for key in ("trace_id", "span_id", "parent_span_id")
            if isinstance(trace.get(key), str) and _HEX.fullmatch(trace[key])
        }
        if ids:
            kept_contexts["trace"] = ids
    runtime = contexts.get("runtime")
    if isinstance(runtime, Mapping):
        kept_contexts["runtime"] = {
            key: runtime[key] for key in ("name", "version") if isinstance(runtime.get(key), str)
        }
    if kept_contexts:
        clean["contexts"] = kept_contexts
    crumbs = (event.get("breadcrumbs") or {}).get("values") if isinstance(event.get("breadcrumbs"), Mapping) else None
    if isinstance(crumbs, list) and crumbs:
        clean["breadcrumbs"] = {"values": [safe_breadcrumb(crumb) for crumb in crumbs if isinstance(crumb, Mapping)]}
    return clean


def before_send(event: Any, hint: Any = None) -> dict[str, Any]:
    return redact_event(event)


def before_breadcrumb(crumb: Any, hint: Any = None) -> dict[str, Any]:
    return safe_breadcrumb(crumb)


def drop_transaction(event: Any, hint: Any = None) -> None:
    return None


def sentry_options() -> dict[str, Any]:
    """Every option that keeps data out; the DSN is the only one taken from configuration."""
    from sentry_sdk.integrations.django import DjangoIntegration

    return {
        "dsn": sentry_dsn(),
        "send_default_pii": False,
        "before_send": before_send,
        "before_breadcrumb": before_breadcrumb,
        "before_send_transaction": drop_transaction,
        "max_request_body_size": "never",
        "include_local_variables": False,
        "include_source_context": False,
        "attach_stacktrace": False,
        "auto_session_tracking": False,
        "traces_sample_rate": None,
        "integrations": [DjangoIntegration(transaction_style="url", middleware_spans=False, signals_spans=False)],
    }


def configure_sentry() -> bool:
    """Called from ``AppConfig.ready()``; a no-op without the DSN or the extra."""
    global _configured
    if _configured or not sentry_enabled():
        return False
    import sentry_sdk

    sentry_sdk.init(**sentry_options())
    _configured = True
    return True
