from skeleton.common.models import SecuredRecord
from skeleton.system.registry import get_spec


def visible_audit_changes(event):
    try:
        spec = get_spec(event.model_label)
    except KeyError:
        return {}
    if not issubclass(spec.model, SecuredRecord):
        # Platform/access lifecycle audit is privileged, not a generic business reader.
        return event.changes
    readable_columns = {spec.model._meta.get_field(name).column for name in spec.read_fields}
    return {name: change for name, change in event.changes.items() if name in readable_columns}


def audit_data(event):
    """REST shape of one ``AuditLogEvent`` (``AuditLogService``).

    Audit identities are deliberately historical, never dereferenced to private objects.
    """
    import json

    data = {
        name: getattr(event, name)
        for name in (
            "sysid",
            "created_at",
            "organization_id",
            "table_name",
            "model_label",
            "record_sysid",
            "record_revision",
            "operation",
            "actor_sysid",
            "request_id",
            "source",
            "db_principal",
        )
    }
    data["changes"] = json.loads(event.changes_json)
    return data
