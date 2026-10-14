"""Optional owned business services; a declared missing operation always denies."""
from dataclasses import dataclass
from inspect import signature
from collections.abc import Callable

from django.apps import apps
from django.core.exceptions import PermissionDenied, ValidationError

from skeleton.common.models import SecuredRecord
from skeleton.system.registry import get_spec


@dataclass(frozen=True)
class RecordOperations:
    create: Callable | None = None
    update: Callable | None = None
    delete: Callable | None = None
    restore: Callable | None = None


_operations = {}


def register_record_operations(table_key, operations):
    """Services accept (record, *, user, values) or (record, *, user) for lifecycle."""
    from skeleton.system.app_registry import get_app
    try:
        spec = get_spec(table_key)
        get_app(spec.model._meta.app_label)
        package = apps.get_app_config(spec.model._meta.app_label).name
    except (KeyError, LookupError):
        raise ValidationError("Record operations require an installed owning application.") from None
    if not isinstance(operations, RecordOperations) or not spec.expose_api or not issubclass(spec.model, SecuredRecord):
        raise ValidationError("Record operations require an exposed business model.")
    for action in ("create", "update", "delete", "restore"):
        service = getattr(operations, action)
        if service is None:
            continue
        module = getattr(service, "__module__", "")
        if not callable(service) or not (module == package or module.startswith(package + ".")):
            raise ValidationError("Business service must belong to its owning application.")
        parameters = {"user": None, **({"values": {}} if action in {"create", "update"} else {})}
        try:
            signature(service).bind(None, **parameters)
        except (TypeError, ValueError):
            raise ValidationError("Invalid business service signature.") from None
    if table_key in _operations and _operations[table_key] != operations:
        raise ValidationError("Duplicate record operations registration.")
    _operations[table_key] = operations
    return operations


def unregister_record_operations(app_label):
    for key in tuple(_operations):
        if key.startswith(app_label + "."):
            del _operations[key]


def require_operation(spec, action):
    operations = _operations.get(spec.model._meta.label_lower)
    if operations is None:
        return None
    service = getattr(operations, action)
    if service is None:
        raise PermissionDenied("This business operation is not supported.")
    return service
