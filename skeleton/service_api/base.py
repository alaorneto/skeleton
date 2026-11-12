"""Base class and decorators of the in-process service API (docs/service-api.md).

A service never opens a context: it runs inside the caller's ``tenant_context`` (or
``system_context`` for ``allow_system`` methods). ``service_method`` refuses an ``actor`` that
differs from ``current_context().actor``, so an app cannot act on behalf of someone else, and
translates internal exceptions into ``skeleton.service_api.errors``.
"""

import dataclasses
import functools
import inspect
import logging
import warnings
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, ClassVar, ParamSpec, TypeVar

from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from django.db import IntegrityError
from django.http import Http404
from django.utils.translation import gettext

from skeleton.common.exceptions import ConcurrentUpdate, LiveReferencesExist, MissingTenantContext
from skeleton.tenancy.context import current_context

from .errors import Conflict, ContextRequired, Forbidden, InvalidInput, NotFound, ServiceError, Unavailable

P = ParamSpec("P")
R = TypeVar("R")

logger = logging.getLogger("skeleton.service_api")

SPEC_ATTRIBUTE = "__service_method__"
DEPRECATION_ATTRIBUTE = "__service_deprecation__"
_MAX_RESULT_DEPTH = 8


@dataclass(frozen=True, slots=True)
class ServiceMethodSpec:
    name: str
    read_only: bool
    allow_system: bool
    tenant: bool
    since: str


@dataclass(frozen=True, slots=True)
class Deprecation:
    since: str
    removal: str


class ServiceAPI:
    """Stateless base of a service class; consumers instantiate it without arguments.

    ``app_label`` names the provider application. When set, every method first requires the
    application to be installed and enabled (with its dependencies) in the organization and
    raises ``Unavailable`` otherwise. Core services leave it ``None``.
    """

    __slots__ = ()
    app_label: ClassVar[str | None] = None

    @classmethod
    def service_methods(cls) -> dict[str, ServiceMethodSpec]:
        result: dict[str, ServiceMethodSpec] = {}
        for name, member in inspect.getmembers(cls, callable):
            spec = getattr(member, SPEC_ATTRIBUTE, None)
            if isinstance(spec, ServiceMethodSpec):
                result[name] = spec
        return result

    @classmethod
    def deprecations(cls) -> dict[str, Deprecation]:
        result: dict[str, Deprecation] = {}
        for name, member in inspect.getmembers(cls, callable):
            deprecation = getattr(member, DEPRECATION_ATTRIBUTE, None)
            if isinstance(deprecation, Deprecation):
                result[name] = deprecation
        return result


def _validate_signature(function: Callable[..., Any]) -> None:
    parameters = list(inspect.signature(function).parameters.values())
    if not parameters or parameters[0].name != "self":
        raise TypeError(f"{function.__qualname__}: service methods are instance methods.")
    for parameter in parameters[1:]:
        if parameter.kind is not inspect.Parameter.KEYWORD_ONLY:
            raise TypeError(f"{function.__qualname__}: service method arguments must be keyword-only.")
    if not any(parameter.name == "actor" for parameter in parameters[1:]):
        raise TypeError(f"{function.__qualname__}: service methods receive the explicit keyword 'actor'.")


def _check_caller(spec: ServiceMethodSpec, actor: object) -> None:
    context = current_context()
    if spec.tenant and context.organization_id is None:
        raise ContextRequired()
    if actor is None:
        if not (spec.allow_system and context.system):
            raise Forbidden(gettext("A operação exige um ator."))
        return
    if context.actor is None or getattr(actor, "pk", None) != context.actor:
        raise Forbidden(gettext("O ator não corresponde ao contexto da chamada."))


def _require_provider(app_label: str) -> None:
    from skeleton.system.app_registry import get_app
    from skeleton.system.applications import require_application_enabled

    try:
        get_app(app_label)
        require_application_enabled(app_label)
    except (KeyError, PermissionDenied):
        raise Unavailable() from None


def _validation_error(exc: ValidationError) -> ServiceError:
    errors = (
        [item for values in exc.error_dict.values() for item in values]
        if hasattr(exc, "error_dict")
        else exc.error_list
    )
    if any(item.code in {"unique", "unique_together"} for item in errors):
        return Conflict("integrity_conflict")
    if hasattr(exc, "error_dict"):
        return InvalidInput(exc.message_dict)
    return InvalidInput({"non_field_errors": exc.messages})


def _translate(exc: Exception) -> ServiceError | None:
    from skeleton.access.services import LastAdministrator

    if isinstance(exc, ConcurrentUpdate):
        return Conflict("stale_revision")
    if isinstance(exc, LiveReferencesExist):
        return Conflict("live_references")
    if isinstance(exc, LastAdministrator):
        return Conflict("last_administrator")
    if isinstance(exc, ValidationError):
        return _validation_error(exc)
    if isinstance(exc, IntegrityError):
        return Conflict("integrity_conflict")
    if isinstance(exc, ObjectDoesNotExist | Http404):
        return NotFound()
    if isinstance(exc, PermissionDenied):
        return Forbidden()
    if isinstance(exc, MissingTenantContext):
        return ContextRequired()
    return None


def require_boundary_value(value: object, *, where: str, depth: int = 0) -> None:
    """Accept only what the contract allows: primitives, text enums, tuples and frozen DTOs (#272).

    An allow-list, the same types ``assert_service_contract`` requires of the annotations: a
    generator over a queryset, a float, bytes or a plain object would otherwise cross the
    boundary, and a lazy value could read the database after the service context ended.
    """
    import enum
    from datetime import date
    from decimal import Decimal
    from uuid import UUID

    if depth > _MAX_RESULT_DEPTH:
        raise TypeError(f"{where}: service result is nested too deeply.")
    if isinstance(value, enum.Enum):
        if not isinstance(value, str):
            raise TypeError(f"{where}: enums crossing the boundary must be text enums.")
        return
    if value is None or isinstance(value, bool | int | str | Decimal | date | UUID):
        return
    if isinstance(value, tuple):
        for item in value:
            require_boundary_value(item, where=where, depth=depth + 1)
        return
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        if not type(value).__dataclass_params__.frozen:  # type: ignore[attr-defined]
            raise TypeError(f"{where}: DTOs must be frozen dataclasses.")
        for field in dataclasses.fields(value):
            require_boundary_value(getattr(value, field.name), where=where, depth=depth + 1)
        return
    raise TypeError(f"{where}: service methods return DTOs, pages, tuples or primitives, not {type(value).__name__}.")


def _run_command[T](function: Callable[..., T], args: tuple[Any, ...], kwargs: dict[str, Any]) -> T:
    """Run a command inside a savepoint of the caller's transaction (#232).

    A command that raises leaves nothing it wrote, and the caller's transaction stays usable.
    Without an open transaction (unit tests with a fake context) the call runs as is: opening
    ``atomic`` there would start and commit a transaction, which a service never does.
    """
    from django.db import connections, transaction

    using = current_context().using
    if not connections[using].in_atomic_block:
        return function(*args, **kwargs)
    with transaction.atomic(using=using):
        return function(*args, **kwargs)


def service_method(
    *, read_only: bool, since: str, allow_system: bool = False, tenant: bool = True
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Mark a public service method and enforce the caller contract on every call.

    - ``actor`` (keyword) must be the actor of ``current_context()``; ``actor=None`` is accepted
      only with ``allow_system=True`` inside ``system_context``.
    - ``tenant=True`` (default) requires an organization in the context (``ContextRequired``).
    - ``read_only`` and ``since`` are contract metadata for checks and documentation.
    """
    if type(read_only) is not bool or type(allow_system) is not bool or type(tenant) is not bool:
        raise TypeError("service_method flags must be booleans.")
    if not isinstance(since, str) or not since:
        raise TypeError("service_method requires the version that introduced it (since=...).")

    def decorate(function: Callable[P, R]) -> Callable[P, R]:
        _validate_signature(function)
        spec = ServiceMethodSpec(
            name=function.__name__, read_only=read_only, allow_system=allow_system, tenant=tenant, since=since
        )

        @functools.wraps(function)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            if len(args) != 1 or not isinstance(args[0], ServiceAPI):
                raise TypeError(f"{function.__qualname__}: call it on a ServiceAPI with keyword arguments.")
            if "actor" not in kwargs:
                raise TypeError(f"{function.__qualname__}: the 'actor' keyword is required.")
            _check_caller(spec, kwargs["actor"])
            provider = type(args[0]).app_label
            try:
                if provider is not None:
                    _require_provider(provider)
                result = function(*args, **kwargs) if spec.read_only else _run_command(function, args, kwargs)
            except ServiceError:
                raise
            except Exception as exc:
                translated = _translate(exc)
                if translated is None:
                    raise
                raise translated from exc
            require_boundary_value(result, where=function.__qualname__)
            return result

        setattr(wrapper, SPEC_ATTRIBUTE, spec)
        return wrapper

    return decorate


def deprecated(*, since: str, removal: str) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Keep a method for at least one minor version before the major that removes it."""
    if not since or not removal:
        raise TypeError("deprecated() requires since=... and removal=....")
    deprecation = Deprecation(since=since, removal=removal)

    def decorate(function: Callable[P, R]) -> Callable[P, R]:
        message = f"{function.__qualname__} is deprecated since {since} and will be removed in {removal}."

        @functools.wraps(function)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            logger.warning(
                "service_api.deprecated_call",
                extra={"service_method": function.__qualname__, "since": since, "removal": removal},
            )
            warnings.warn(message, DeprecationWarning, stacklevel=2)
            return function(*args, **kwargs)

        setattr(wrapper, DEPRECATION_ATTRIBUTE, deprecation)
        return wrapper

    return decorate
