from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from uuid import uuid4

from .logging import security_event

# The correlation id of the request or job execution running in this thread. Tracing (#176)
# and Sentry (#177) read it; it is never accepted from a client header.
_request_id: ContextVar[str | None] = ContextVar("skeleton_request_id", default=None)


def current_request_id() -> str | None:
    """The ``request_id`` of the request (or job execution) in progress, if any."""
    return _request_id.get()


@contextmanager
def bound_request_id(value) -> Iterator[str]:
    """Make ``value`` the current correlation id until the block ends."""
    token = _request_id.set(str(value))
    try:
        yield str(value)
    finally:
        _request_id.reset(token)


def outcome(status):
    if status >= 500:
        return "server_error"
    if status in {409, 412, 428}:
        return "conflict"
    if status in {401, 403}:
        return "access_denied"
    if status == 404:
        return "not_found"
    if status >= 400:
        return "validation_error"
    return "success"


class RequestCompletionMiddleware:
    """Outer middleware observes the response after transactions and commit callbacks."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.request_id = uuid4()
        with bound_request_id(request.request_id):
            response = self.get_response(request)
        response["X-Request-ID"] = str(request.request_id)
        failed_delivery = bool(getattr(request, "_skeleton_mail_failures", 0))
        if failed_delivery:
            response["X-Email-Delivery"] = "failed"
        security_event(
            "http.request",
            status=response.status_code,
            outcome=outcome(response.status_code),
            request_id=str(request.request_id),
            source="http",
            context_snapshot=getattr(request, "_skeleton_log_context", None),
            reason_code="email_delivery_failed" if failed_delivery else None,
        )
        return response
