from django.apps import apps
from django.core.exceptions import ValidationError

from skeleton.web.services import assert_fields, coerce_fields, record_data

DEDICATED = {
    "memberships": ("sk_tenancy.Membership", ("user", "is_active")),
    "roles": ("sk_access.Role", ("code", "name", "description")),
    "groups": ("sk_access.AccessGroup", ("code", "name", "description")),
    "invitations": ("sk_tenancy.Invitation", ("email", "role")),
    "member-roles": ("sk_access.MemberRole", ("membership", "role")),
    "group-members": ("sk_access.GroupMember", ("group", "membership")),
    "group-roles": ("sk_access.GroupRole", ("group", "role")),
    "table-permissions": ("sk_access.TablePermission", ("role", "table", "action")),
    "record-grants": ("sk_access.RecordGrant", ("table", "record_sysid", "membership", "group", "role", "action")),
}
LINKS = frozenset({"member-roles", "group-members", "group-roles", "table-permissions", "record-grants"})


def dedicated_model(resource):
    if resource not in DEDICATED:
        from django.http import Http404
        raise Http404
    label, fields = DEDICATED[resource]
    return apps.get_model(label), fields


def dedicated_input(resource, data, *, user, creating=True):
    model, fields = dedicated_model(resource)
    if resource == "memberships" and not creating:
        fields = ("is_active",)
    assert_fields(data, fields)
    if creating:
        required = {"memberships": ("user",), "roles": ("code", "name"), "groups": ("code", "name"), "invitations": ("email", "role")}.get(resource, tuple(name for name in fields if not model._meta.get_field(name).null))
        missing = [name for name in required if name not in data]
        if missing:
            raise ValidationError({name: "Campo obrigatório." for name in missing})
    values = dict(data)
    if resource == "memberships" and "user" in values:
        # No global identity list: an administrator must select an existing UUID explicitly.
        User = apps.get_model("sk_accounts", "User")
        try:
            values["user"] = User.objects.get(pk=values["user"], is_active=True)
        except (User.DoesNotExist, ValidationError, ValueError, TypeError):
            raise ValidationError({"user": "Identidade indisponível."}) from None
        rest = {name: value for name, value in values.items() if name != "user"}
        return {"user": values["user"], **coerce_fields(model, rest, user=user)}
    return coerce_fields(model, values, user=user)


def dedicated_data(resource, record):
    _, fields = dedicated_model(resource)
    extra = {"roles": ("is_builtin",), "invitations": ("expires_at", "accepted_at", "accepted_by")}.get(resource, ())
    return record_data(record, (*fields, *extra))


def profile_data(user, profile):
    return {
        "sysid": user.sysid, "email": user.email, "first_name": user.first_name,
        "last_name": user.last_name, "revision": user.revision,
        "profile": {"sysid": profile.sysid, "revision": profile.revision,
                    "display_name": profile.display_name, "timezone": profile.timezone, "language": profile.language},
    }


def audit_data(event):
    # Audit identities are deliberately historical, never dereferenced to private objects.
    from skeleton.audit.presentation import visible_audit_changes
    data = {name: getattr(event, name) for name in (
        "sysid", "created_at", "organization_id", "table_name", "model_label", "record_sysid",
        "record_revision", "operation", "actor_sysid", "request_id", "source", "db_principal",
    )}
    data["changes"] = visible_audit_changes(event)
    return data
