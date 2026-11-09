import re

from django.core.exceptions import ValidationError
from django.db import IntegrityError
from rest_framework.exceptions import APIException
from rest_framework.views import exception_handler as drf_exception_handler


class BoundaryError(APIException):
    def __init__(self, code, detail, status=400, fields=None):
        self.status_code = status
        self.detail = {"code": code, "detail": detail}
        if fields:
            self.detail["fields"] = fields


def parse_revision(value, *, composite=False):
    if value is None:
        raise BoundaryError("revision_required", "Envie If-Match com a revisão atual.", 428)
    pattern = r'"([1-9][0-9]*)-([1-9][0-9]*)"' if composite else r'"([1-9][0-9]*)"'
    match = re.fullmatch(pattern, value)
    if not match:
        raise BoundaryError("invalid_revision", "If-Match inválido.")
    revisions = tuple(int(item) for item in match.groups())
    return revisions if composite else revisions[0]


def require_revision(request, record):
    if parse_revision(request.headers.get("If-Match")) != record.revision:
        raise BoundaryError("revision_conflict", "O registro foi alterado por outra operação.", 412)


def validation_conflict(error):
    errors = (
        [item for values in error.error_dict.values() for item in values]
        if hasattr(error, "error_dict")
        else error.error_list
    )
    return any(item.code in {"unique", "unique_together"} for item in errors)


def service_error_response(exc):
    """Map a public service API error to the REST boundary; ``ContextRequired`` stays a 500."""
    from skeleton.service_api import errors

    if isinstance(exc, errors.NotFound | errors.Unavailable):
        # Unavailable is neutral on purpose: a disabled integration looks like a missing route.
        return BoundaryError("not_found", "Not found.", 404)
    if isinstance(exc, errors.Forbidden):
        return BoundaryError("permission_denied", exc.detail, 403)
    if isinstance(exc, errors.InvalidInput):
        return BoundaryError(
            "validation_error", exc.detail, 400, {field: list(items) for field, items in exc.errors.items()}
        )
    if isinstance(exc, errors.Conflict):
        if exc.code == "stale_revision":
            # The same answer as the gateway and the dedicated resources for a stale If-Match.
            return BoundaryError("revision_conflict", "O registro foi alterado por outra operação.", 412)
        return BoundaryError(exc.code, exc.detail, 409)
    return None


def exception_handler(exc, context):
    from skeleton.access.services import LastAdministrator
    from skeleton.accounts.platform import LastPlatformAdministrator
    from skeleton.common.exceptions import ApplicationUnavailable, ConcurrentUpdate, LiveReferencesExist
    from skeleton.service_api.errors import ServiceError

    if isinstance(exc, ApplicationUnavailable):
        exc = BoundaryError("not_found", "Not found.", 404)
    if isinstance(exc, ServiceError):
        mapped = service_error_response(exc)
        if mapped is None:
            return None  # ContextRequired and unknown service errors are programming errors (500).
        exc = mapped
    if isinstance(exc, ConcurrentUpdate):
        exc = BoundaryError("revision_conflict", "O registro foi alterado por outra operação.", 412)
    elif isinstance(exc, LiveReferencesExist):
        exc = BoundaryError("live_references", "Existem vínculos ativos com este registro.", 409)
    elif isinstance(exc, LastAdministrator):
        exc = BoundaryError("last_administrator", "A organização precisa manter ao menos um administrador ativo.", 409)
    elif isinstance(exc, LastPlatformAdministrator):
        exc = BoundaryError(
            "last_platform_administrator", "A plataforma precisa manter ao menos um administrador ativo.", 409
        )
    elif isinstance(exc, ValidationError):
        fields = exc.message_dict if hasattr(exc, "message_dict") else {"non_field_errors": exc.messages}
        conflict = validation_conflict(exc)
        exc = BoundaryError(
            "integrity_conflict" if conflict else "validation_error",
            "Verifique os campos informados.",
            409 if conflict else 400,
            fields,
        )
    elif isinstance(exc, IntegrityError):
        exc = BoundaryError("integrity_conflict", "A operação conflita com os dados existentes.", 409)
    response = drf_exception_handler(exc, context)
    if response is not None and not isinstance(exc, BoundaryError):
        codes = {
            400: "invalid_request",
            403: "permission_denied",
            404: "not_found",
            405: "method_not_allowed",
            429: "rate_limited",
        }
        response.data = {
            "code": codes.get(response.status_code, "request_failed"),
            "detail": response.data.get("detail", "A operação não pôde ser concluída."),
        }
    return response
