"""Public, stable errors of the service API (docs/service-api.md, "Erros").

Internal exceptions never cross the service boundary: ``service_method`` translates the
known ones into these classes. The REST mapping lives in ``skeleton.api.errors``.
"""

from collections.abc import Iterable, Mapping
from types import MappingProxyType

from django.utils.translation import gettext_lazy


class ServiceError(Exception):
    """Base of every error a service method may raise to its consumer."""

    default_code = "service_error"
    default_detail = gettext_lazy("A operação não pôde ser concluída.")

    def __init__(self, detail: str | None = None, *, code: str | None = None) -> None:
        self.code = code or self.default_code
        self.detail = detail or self.default_detail
        super().__init__(self.detail)


class NotFound(ServiceError):
    """Target is missing, belongs to another organization or is not readable by the actor."""

    default_code = "not_found"
    default_detail = gettext_lazy("Não encontrado.")


class Forbidden(ServiceError):
    """The actor may read the target but not perform the action, or diverges from the context."""

    default_code = "forbidden"
    default_detail = gettext_lazy("Você não tem permissão para esta operação.")


class InvalidInput(ServiceError):
    """Field validation failed; ``errors`` maps each field to its messages."""

    default_code = "invalid_input"
    default_detail = gettext_lazy("Verifique os campos informados.")

    def __init__(self, errors: Mapping[str, Iterable[str]], detail: str | None = None) -> None:
        self.errors: Mapping[str, tuple[str, ...]] = MappingProxyType(
            {str(field): tuple(str(message) for message in messages) for field, messages in errors.items()}
        )
        super().__init__(detail)


class Conflict(ServiceError):
    """Stale revision (``stale_revision``), uniqueness, last administrator or quota."""

    default_code = "conflict"
    default_detail = gettext_lazy("A operação conflita com o estado atual.")

    def __init__(self, code: str, detail: str | None = None) -> None:
        if not code:
            raise ValueError("Conflict requires a stable code.")
        super().__init__(detail, code=code)


class Unavailable(ServiceError):
    """The provider application is disabled, unavailable or not installed in the organization."""

    default_code = "unavailable"
    default_detail = gettext_lazy("Integração indisponível nesta organização.")


class ContextRequired(ServiceError):
    """Called without the required context: a programming error, never a client error."""

    default_code = "context_required"
    default_detail = gettext_lazy("A chamada exige um contexto de organização.")
