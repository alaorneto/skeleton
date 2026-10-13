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
    errors = [item for values in error.error_dict.values() for item in values] if hasattr(error, "error_dict") else error.error_list
    return any(item.code in {"unique", "unique_together"} for item in errors)


def exception_handler(exc, context):
    from skeleton.common.exceptions import ConcurrentUpdate, LiveReferencesExist
    from skeleton.access.services import LastAdministrator

    if isinstance(exc, ConcurrentUpdate):
        exc = BoundaryError("revision_conflict", "O registro foi alterado por outra operação.", 412)
    elif isinstance(exc, LiveReferencesExist):
        exc = BoundaryError("live_references", "Existem vínculos ativos com este registro.", 409)
    elif isinstance(exc, LastAdministrator):
        exc = BoundaryError("last_administrator", "A organização precisa manter ao menos um administrador ativo.", 409)
    elif isinstance(exc, ValidationError):
        fields = exc.message_dict if hasattr(exc, "message_dict") else {"non_field_errors": exc.messages}
        conflict = validation_conflict(exc)
        exc = BoundaryError("integrity_conflict" if conflict else "validation_error", "Verifique os campos informados.", 409 if conflict else 400, fields)
    elif isinstance(exc, IntegrityError):
        exc = BoundaryError("integrity_conflict", "A operação conflita com os dados existentes.", 409)
    response = drf_exception_handler(exc, context)
    if response is not None and not isinstance(exc, BoundaryError):
        codes = {400: "invalid_request", 403: "permission_denied", 404: "not_found", 405: "method_not_allowed", 429: "rate_limited"}
        response.data = {"code": codes.get(response.status_code, "request_failed"), "detail": response.data.get("detail", "A operação não pôde ser concluída.")}
    return response
