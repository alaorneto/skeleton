"""Bounded reference selection; source capability and target policy are independent."""

from django.apps import apps
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404
from django.utils.translation import gettext

from skeleton.access.policies import authorized_queryset, is_organization_admin
from skeleton.common.models import OrganizationRecord, SecuredRecord
from skeleton.service_api.dto import MAX_OFFSET
from skeleton.system.registry import get_spec

ACCESS_REFERENCE_FIELDS = frozenset({"owner", "owning_group"})
CORE_REFERENCE_LABELS = frozenset(
    {
        "sk_tenancy.membership",
        "sk_access.role",
        "sk_access.accessgroup",
    }
)


def target_queryset(field, *, user, source_model, access=False):
    """Fail closed for a target without a readable policy; never list global identities."""
    related = field.remote_field.model
    if issubclass(related, SecuredRecord):
        return authorized_queryset(related, user=user)
    if related._meta.label_lower == "sk_accounts.user":
        membership = apps.get_model("sk_tenancy", "Membership")
        active = membership.objects.filter(is_active=True).values("user_id")
        return related.objects.filter(sysid__in=active, is_active=True)
    if issubclass(related, OrganizationRecord):
        if related._meta.label_lower not in CORE_REFERENCE_LABELS:
            raise PermissionDenied("Reference target has no readable policy.")
        if (
            related._meta.label_lower == "sk_access.role"
            and source_model._meta.label_lower == "sk_tenancy.invitation"
            and not is_organization_admin(user)
        ):
            return _invitation_roles(related, user=user)
        if not (access and related._meta.label_lower == "sk_access.accessgroup") and not is_organization_admin(user):
            raise PermissionDenied("Organization administration required for this reference.")
        queryset = related.objects.all()
        if related._meta.label_lower == "sk_tenancy.membership":
            queryset = queryset.filter(is_active=True, user__is_active=True, user__deleted_at__isnull=True)
        return queryset
    if related._meta.label_lower == "sk_system.tabledefinition" and not issubclass(source_model, SecuredRecord):
        if not is_organization_admin(user):
            raise PermissionDenied("Organization administration required.")
        from skeleton.records.services import readable_specs

        return related.objects.filter(
            available=True, model_label__in=[spec.model._meta.label_lower for spec in readable_specs(user)]
        )
    raise PermissionDenied("Reference target has no readable policy.")


def _invitation_roles(model, *, user):
    """A delegated inviter picks only roles that hand out no capability."""
    from skeleton.access.capabilities import BUILTIN_ROLES, INVITATIONS_MANAGE, require_capability

    require_capability(user, INVITATIONS_MANAGE)
    privileged = [code for code, role in BUILTIN_ROLES.items() if role.capabilities]
    return model.objects.exclude(is_builtin=True, code__in=privileged)


def reference_queryset(spec, field_name, *, user, record=None, access=False):
    from skeleton.records.services import ACCESS_FIELDS, record_queryset, require_access_management

    if access:
        if record is None or field_name not in ACCESS_REFERENCE_FIELDS:
            raise ValidationError("Reference selection requires an access-managed record.")
        require_access_management(record, user=user)
        names = ACCESS_FIELDS
    else:
        names = spec.write_fields
        if record is None:
            record_queryset(spec, user=user, action="create")
        elif not record_queryset(spec, user=user, action="update").filter(pk=record.pk).exists():
            raise Http404
    if record is not None and record._meta.label_lower != spec.model._meta.label_lower:
        raise Http404
    if field_name not in names:
        raise Http404
    field = spec.model._meta.get_field(field_name)
    if not field.is_relation or field.many_to_many or field.one_to_many:
        raise Http404
    return target_queryset(field, user=user, source_model=spec.model, access=access).order_by("created_at", "sysid")


def label_field(model):
    """Return a scalar allowlisted title, or None; do not call business __str__."""
    if issubclass(model, SecuredRecord):
        spec = get_spec(model._meta.label_lower)
        from skeleton.system.presentation import get_model_ui

        requested = get_model_ui(model._meta.label_lower).list.title_field
        candidates = (requested,) if requested else ("title", "name", "code")
        for name in candidates:
            if name and name in spec.read_fields:
                field = model._meta.get_field(name)
                if not field.is_relation:
                    return name
    elif model._meta.label_lower in {"sk_access.role", "sk_access.accessgroup"}:
        return "name"
    elif model._meta.label_lower == "sk_system.tabledefinition":
        return "label"
    return None


def reference_label(record, *, user):
    name = label_field(type(record))
    value = getattr(record, name) if name else None
    return str(value) if value is not None and value != "" else str(record.pk)


def reference_options(spec, field_name, *, user, record=None, access=False, page=1, page_size=25, term=None):
    if (
        type(page) is not int
        or page < 1
        or type(page_size) is not int
        or not 1 <= page_size <= 100
        or (page - 1) * page_size > MAX_OFFSET
    ):
        raise ValidationError(gettext("Paginação inválida."))
    queryset = reference_queryset(spec, field_name, user=user, record=record, access=access)
    name = label_field(queryset.model)
    if term is not None and term != "":
        # Prefix and, with pg_trgm, similarity on the label (#181); still only readable targets.
        from skeleton.system.search import apply_label_search

        queryset = apply_label_search(queryset, name, term)
    count = queryset.count()
    queryset = queryset.only("sysid", *([name] if name else []))
    offset = (page - 1) * page_size
    return {
        "count": count,
        "page": page,
        "page_size": page_size,
        "results": [
            {"sysid": str(item.pk), "label": reference_label(item, user=user)}
            for item in queryset[offset : offset + page_size]
        ],
    }


def reference_request(spec, field_name, *, user, params):
    """Parse the same constrained picker request for HTML and API."""
    if set(params) - {"record", "access", "page", "page_size", "q"}:
        raise ValidationError(gettext("Parâmetros inválidos."))
    if hasattr(params, "getlist") and any(len(params.getlist(key)) != 1 for key in params):
        raise ValidationError(gettext("Parâmetros repetidos."))
    if params.get("access", "0") not in {"0", "1"}:
        raise ValidationError(gettext("Ação inválida."))
    access = params.get("access") == "1"
    record = None
    from skeleton.records.services import record_queryset

    if params.get("record"):
        try:
            identity = spec.model._meta.pk.to_python(params["record"])
        except (ValidationError, ValueError, TypeError):
            raise ValidationError(gettext("Registro de origem inválido.")) from None
        record = (
            record_queryset(spec, user=user, action="manage_access" if access else "update").filter(pk=identity).first()
        )
        if record is None:
            raise Http404
    try:
        page = int(params.get("page", "1"))
        size = int(params.get("page_size", "25"))
    except (ValueError, TypeError):
        raise ValidationError(gettext("Paginação inválida.")) from None
    return (
        reference_options(
            spec, field_name, user=user, record=record, access=access, page=page, page_size=size, term=params.get("q")
        ),
        record,
        access,
    )
