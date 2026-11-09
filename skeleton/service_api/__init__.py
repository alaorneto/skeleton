"""In-process service API shared by applications (docs/service-api.md)."""

from .base import Deprecation, ServiceAPI, ServiceMethodSpec, deprecated, service_method
from .dto import MAX_PAGE_SIZE, Page, as_utc, as_uuid, paginate
from .errors import Conflict, ContextRequired, Forbidden, InvalidInput, NotFound, ServiceError, Unavailable

__all__ = [
    "MAX_PAGE_SIZE",
    "Conflict",
    "ContextRequired",
    "Deprecation",
    "Forbidden",
    "InvalidInput",
    "NotFound",
    "Page",
    "ServiceAPI",
    "ServiceError",
    "ServiceMethodSpec",
    "Unavailable",
    "as_utc",
    "as_uuid",
    "deprecated",
    "paginate",
    "service_method",
]
