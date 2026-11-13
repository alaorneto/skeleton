from django.core.exceptions import PermissionDenied, ValidationError
from django.db import models, transaction
from django.http import Http404
from django.utils.translation import gettext

from skeleton.access.policies import authorized_queryset, is_organization_admin, require_record_action
from skeleton.common.models import ACCESS_FIELD_NAMES, SYSTEM_FIELD_NAMES, SecuredRecord
from skeleton.system.registry import all_specs, get_spec
from skeleton.tenancy.context import current_context

SYSTEM_FIELDS = SYSTEM_FIELD_NAMES  # Single source in skeleton.common.models (#310).
ACCESS_FIELDS = ACCESS_FIELD_NAMES


def exposed_spec(key):
    try:
        spec = get_spec(key)
    except (KeyError, LookupError):
        raise Http404 from None
    if spec is None or not spec.expose_api or not issubclass(spec.model, SecuredRecord):
        raise Http404
    return spec


def readable_specs(user):
    """Exposed business tables the actor may read: the answer of ``authorized_queryset`` per table.

    One query of table permissions for every table (``table_permissions``: same roles, catalog
    availability and token scopes) and the app check once per app, instead of a whole
    authorization per table (#302). Nothing is cached beyond this call.
    """
    from skeleton.access.policies import table_permissions
    from skeleton.system.applications import require_application_enabled

    candidates = [spec for spec in all_specs() if spec.expose_api and issubclass(spec.model, SecuredRecord)]
    if not candidates:
        return []
    permitted = table_permissions(user)
    enabled = {}
    result = []
    for spec in candidates:
        if (spec.model._meta.label_lower, "read") not in permitted:
            continue
        app_label = spec.model._meta.app_label
        if app_label not in enabled:
            try:
                require_application_enabled(app_label)
                enabled[app_label] = True
            except PermissionDenied:  # ApplicationUnavailable included.
                enabled[app_label] = False
        if enabled[app_label]:
            result.append(spec)
    return result


def assert_fields(data, allowed):
    if not isinstance(data, dict):
        raise ValidationError(gettext("Envie um objeto de campos."))
    unknown = set(data) - set(allowed)
    if unknown:
        raise ValidationError({key: gettext("Campo não permitido.") for key in sorted(unknown)})


def coerce_fields(model, data, *, user):
    values = {}
    for name, value in data.items():
        field = model._meta.get_field(name)
        if field.is_relation:
            if value is None and field.null:
                values[name] = None
                continue
            related = field.remote_field.model
            try:
                value = field.target_field.to_python(value)
            except (ValidationError, ValueError, TypeError):
                raise ValidationError({name: gettext("Referência inválida.")}) from None
            from skeleton.records.references import target_queryset

            queryset = target_queryset(field, user=user, source_model=model, access=name in ACCESS_FIELDS)
            try:
                values[name] = queryset.get(pk=field.target_field.to_python(value))
            except (related.DoesNotExist, ValidationError, ValueError, TypeError):
                raise ValidationError({name: gettext("Referência indisponível.")}) from None
        else:
            try:
                if isinstance(field, (models.CharField, models.TextField)) and not isinstance(value, str):
                    raise ValidationError(gettext("Use um valor textual."))
                if (
                    isinstance(field, models.BooleanField)
                    and not isinstance(value, bool)
                    and not (value is None and field.null)
                ):
                    raise ValidationError(gettext("Use true ou false."))
                values[name] = field.clean(value, None)
            except (ValidationError, ValueError, TypeError) as exc:
                raise ValidationError({name: getattr(exc, "messages", [gettext("Valor inválido.")])}) from None
    return values


def record_data(record, fields, *, user=None):
    if user is not None:
        return records_data([record], fields, user=user)[0]
    result = {}
    for name in dict.fromkeys((*SYSTEM_FIELDS, *fields)):
        field = record._meta.get_field(name)
        value = getattr(record, field.attname)
        result[name] = value
    return result


def records_data(records, fields, *, user):
    """Project a page with one visibility query per target policy, never per row."""
    items = list(records)
    if not items:
        return []
    names = tuple(dict.fromkeys((*SYSTEM_FIELDS, *fields)))
    result = [record_data(record, names) for record in items]
    from skeleton.records.references import target_queryset

    model = type(items[0])
    relations = {}
    for name in names:
        field = model._meta.get_field(name)
        if field.is_relation:
            key = (field.remote_field.model, name in ACCESS_FIELDS)
            relations.setdefault(key, []).append(field)
    for (_, access), group in relations.items():
        ids = {row[field.name] for row in result for field in group if row[field.name] is not None}
        if not ids:
            continue
        try:
            visible = set(
                target_queryset(group[0], user=user, source_model=model, access=access)
                .filter(pk__in=ids)
                .values_list("pk", flat=True)
            )
        except PermissionDenied:
            visible = set()
        for row in result:
            for field in group:
                if row[field.name] not in visible:
                    row[field.name] = None
    return result


def record_queryset(spec, *, user, action="read", include_deleted=False):
    if action in {"create", "update", "delete", "restore"}:
        from skeleton.system.record_operations import require_operation

        require_operation(spec, action)
    return authorized_queryset(spec.model, user=user, action=action, include_deleted=include_deleted)


def filter_records(queryset, spec, params, *, ignore_empty=False):
    scalar = {
        name
        for name in spec.read_fields
        if not spec.model._meta.get_field(name).is_relation
        and not isinstance(spec.model._meta.get_field(name), models.JSONField)
    }
    allowed = scalar | {"sysid", "owner", "visibility", "page", "page_size"}
    # ``q``: full-text search on the declared search_fields (#180), after the authorization.
    if getattr(spec, "search_fields", ()):
        allowed = allowed | {"q"}
    if set(params) - allowed:
        raise ValidationError(gettext("Filtro não permitido."))
    for name in set(params) - {"page", "page_size", "q"}:
        if ignore_empty and params[name] == "":
            continue
        field = spec.model._meta.get_field(name)
        try:
            value = field.target_field.to_python(params[name]) if field.is_relation else field.to_python(params[name])
            queryset = queryset.filter(**{name: value})
        except (ValidationError, ValueError, TypeError):
            raise ValidationError({name: gettext("Filtro inválido.")}) from None
    term = params.get("q") if "q" in params else None
    if term is not None and not (ignore_empty and term == ""):
        from skeleton.system.search import apply_search

        queryset = apply_search(queryset, spec, term)
    return queryset


@transaction.atomic
def create_record(spec, *, user, data):
    assert_fields(data, spec.write_fields)
    record_queryset(spec, user=user, action="create")
    values = coerce_fields(spec.model, data, user=user)
    record = spec.model(organization_id=current_context().organization_id, owner=user, visibility="private", **values)
    if spec.sequence_fields:
        from skeleton.sequences.services import next_number

        # In this atomic block: a refused save rolls the counter back, so no number is lost (#156).
        for name, key in spec.sequence_fields:
            setattr(record, name, next_number(key).formatted)
    from skeleton.system.record_operations import require_operation

    service = require_operation(spec, "create")
    if service is None:
        record.save()
    else:
        service(record, user=user, values=values)
        if record._state.adding:
            raise ValidationError("Business creation service did not persist its record.")
    return record


@transaction.atomic
def update_record(record, spec, *, user, data):
    assert_fields(data, spec.write_fields)
    require_record_action(record, user=user, action="update")
    from skeleton.system.record_operations import require_operation

    service = require_operation(spec, "update")
    values = coerce_fields(spec.model, data, user=user)
    if service is None:
        for name, value in values.items():
            setattr(record, name, value)
        record.save(update_fields=set(values))
    else:
        service(record, user=user, values=values)
    return record


def require_access_management(record, *, user):
    record_queryset(exposed_spec(record._meta.label_lower), user=user, action="manage_access")
    if record.owner_id != user.pk and not is_organization_admin(user):
        raise PermissionDenied


def record_permissions(record, spec, *, user):
    """Which record actions the detail page may offer; every POST still revalidates (#302).

    ``require_record_action`` already builds the authorized queryset of the action, so no
    separate ``record_queryset`` is assembled just to be thrown away.
    """
    from skeleton.system.record_operations import require_operation

    result = {}
    for action in ("update", "delete"):
        try:
            require_operation(spec, action)
            require_record_action(record, user=user, action=action)
            result[action] = True
        except PermissionDenied:
            result[action] = False
    try:
        require_access_management(record, user=user)
        result["manage_access"] = True
    except PermissionDenied:
        result["manage_access"] = False
    return result


@transaction.atomic
def change_record_access(record, *, user, data):
    assert_fields(data, ACCESS_FIELDS)
    require_access_management(record, user=user)
    values = coerce_fields(type(record), data, user=user)
    for name, value in values.items():
        setattr(record, name, value)
    record.save(update_fields=set(values))
    return record


@transaction.atomic
def delete_record(record, *, user):
    return _record_lifecycle(record, user=user, action="delete")


@transaction.atomic
def restore_record(record, *, user):
    return _record_lifecycle(record, user=user, action="restore")


def _record_lifecycle(record, *, user, action):
    spec = exposed_spec(record._meta.label_lower)
    require_record_action(record, user=user, action=action)
    from skeleton.system.record_operations import require_operation

    service = require_operation(spec, action)
    if service is None:
        getattr(record, action)()
    else:
        service(record, user=user)
    return record
