"""Administrative resources of an organization: one dispatch for the HTML and REST surfaces (#265).

Each URL resource name (``memberships``, ``roles`` …) maps to its model, its fields and the
service of ``skeleton.access.services`` that creates, updates or removes it. The surfaces keep
their own refusals (unknown resource, update of a link or invitation), the organization lock,
the revision check and the response; only the choice of service lives here.
"""

from django.apps import apps

from . import services

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

_CREATE = {
    "memberships": "create_membership",
    "roles": "create_role",
    "groups": "create_group",
    "member-roles": "assign_member_role",
    "group-members": "add_group_member",
    "group-roles": "assign_group_role",
    "table-permissions": "grant_table_permission",
    "record-grants": "create_record_grant",
}
_UPDATE = {"memberships": "update_membership", "roles": "update_role", "groups": "update_group"}
_DELETE = {"memberships": "delete_membership", "roles": "delete_role", "groups": "delete_group"}


def dedicated_model(resource):
    if resource not in DEDICATED:
        from django.http import Http404

        raise Http404
    label, fields = DEDICATED[resource]
    return apps.get_model(label), fields


def create_resource(resource, *, user, data, request=None):
    """Create one dedicated resource; ``data`` is already coerced by the surface."""
    if resource == "invitations":
        from skeleton.tenancy.invitations import issue_invitation

        return issue_invitation(user=user, request=request, **data)
    return getattr(services, _CREATE[resource])(user=user, data=data)


def update_resource(resource, record, *, user, data):
    return getattr(services, _UPDATE[resource])(record, user=user, data=data)


def delete_resource(resource, record, *, user):
    if resource in LINKS:
        return services.revoke_link(record, user=user)
    if resource == "invitations":
        from skeleton.tenancy.invitations import revoke_invitation

        return revoke_invitation(record, user=user)
    return getattr(services, _DELETE[resource])(record, user=user)
