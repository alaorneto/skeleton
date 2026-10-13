from django.apps import apps
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import models, transaction
from django.http import Http404

from skeleton.access.policies import authorized_queryset, is_organization_admin, require_record_action
from skeleton.common.models import SecuredRecord
from skeleton.system.registry import all_specs, get_spec
from skeleton.tenancy.context import current_context

SYSTEM_FIELDS = ("sysid", "created_at", "updated_at", "created_by", "updated_by", "deleted_at", "deleted_by", "revision")
ACCESS_FIELDS = ("owner", "visibility", "owning_group")


def exposed_spec(key):
    try:
        spec = get_spec(key)
    except (KeyError, LookupError):
        raise Http404 from None
    if spec is None or not spec.expose_api or not issubclass(spec.model, SecuredRecord):
        raise Http404
    return spec


def readable_specs(user):
    result = []
    for spec in all_specs():
        if not spec.expose_api or not issubclass(spec.model, SecuredRecord):
            continue
        try:
            authorized_queryset(spec.model, user=user)
        except PermissionDenied:
            continue
        result.append(spec)
    return result


def assert_fields(data, allowed):
    if not isinstance(data, dict):
        raise ValidationError("Envie um objeto de campos.")
    unknown = set(data) - set(allowed)
    if unknown:
        raise ValidationError({key: "Campo não permitido." for key in sorted(unknown)})


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
                raise ValidationError({name: "Referência inválida."}) from None
            if issubclass(related, SecuredRecord):
                queryset = authorized_queryset(related, user=user)
            elif related._meta.label_lower == "sk_accounts.user":
                membership = apps.get_model("sk_tenancy", "Membership")
                active = membership.objects.filter(user_id=value, is_active=True).exists()
                if not active:
                    raise ValidationError({name: "Usuário não é membro ativo desta organização."})
                queryset = related.objects.filter(is_active=True)
            else:
                queryset = related.objects.all()
            try:
                values[name] = queryset.get(pk=field.target_field.to_python(value))
            except (related.DoesNotExist, ValidationError, ValueError, TypeError):
                raise ValidationError({name: "Referência indisponível."}) from None
        else:
            try:
                if isinstance(field, (models.CharField, models.TextField)) and not isinstance(value, str):
                    raise ValidationError("Use um valor textual.")
                if isinstance(field, models.BooleanField) and not isinstance(value, bool):
                    raise ValidationError("Use true ou false.")
                values[name] = field.clean(value, None)
            except (ValidationError, ValueError, TypeError) as exc:
                raise ValidationError({name: getattr(exc, "messages", ["Valor inválido."])}) from None
    return values


def record_data(record, fields, *, user=None):
    result = {}
    for name in dict.fromkeys((*SYSTEM_FIELDS, *fields)):
        field = record._meta.get_field(name)
        value = getattr(record, field.attname)
        if field.is_relation and value is not None and user is not None:
            related = field.remote_field.model
            if issubclass(related, SecuredRecord):
                try:
                    visible = authorized_queryset(related, user=user).filter(pk=value).exists()
                except PermissionDenied:
                    visible = False
                if not visible:
                    value = None
            elif hasattr(related, "deleted_at") and related._meta.label_lower != "sk_accounts.user":
                if not related.objects.filter(pk=value).exists():
                    value = None
        result[name] = value
    return result


def record_queryset(spec, *, user, action="read", include_deleted=False):
    return authorized_queryset(spec.model, user=user, action=action, include_deleted=include_deleted)


def filter_records(queryset, spec, params):
    scalar = {name for name in spec.read_fields
              if not spec.model._meta.get_field(name).is_relation
              and not isinstance(spec.model._meta.get_field(name), models.JSONField)}
    allowed = scalar | {"sysid", "owner", "visibility", "page", "page_size"}
    if set(params) - allowed:
        raise ValidationError("Filtro não permitido.")
    for name in set(params) - {"page", "page_size"}:
        field = spec.model._meta.get_field(name)
        try:
            value = field.target_field.to_python(params[name]) if field.is_relation else field.to_python(params[name])
            queryset = queryset.filter(**{name: value})
        except (ValidationError, ValueError, TypeError):
            raise ValidationError({name: "Filtro inválido."}) from None
    return queryset


@transaction.atomic
def create_record(spec, *, user, data):
    assert_fields(data, spec.write_fields)
    record_queryset(spec, user=user, action="create")
    record = spec.model(organization_id=current_context().organization_id, owner=user, visibility="private", **coerce_fields(spec.model, data, user=user))
    record.save()
    return record


@transaction.atomic
def update_record(record, spec, *, user, data):
    assert_fields(data, spec.write_fields)
    require_record_action(record, user=user, action="update")
    values = coerce_fields(spec.model, data, user=user)
    for name, value in values.items():
        setattr(record, name, value)
    record.save(update_fields=set(values))
    return record


def require_access_management(record, *, user):
    record_queryset(exposed_spec(record._meta.label_lower), user=user, action="manage_access")
    if record.owner_id != user.pk and not is_organization_admin(user):
        raise PermissionDenied


@transaction.atomic
def change_record_access(record, *, user, data):
    assert_fields(data, ACCESS_FIELDS)
    require_access_management(record, user=user)
    values = coerce_fields(type(record), data, user=user)
    for name, value in values.items():
        setattr(record, name, value)
    record.save(update_fields=set(values))
    return record
