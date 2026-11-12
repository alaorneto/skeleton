"""Migration-owned guards, tenant policies and transactional row auditing.

Only historical migration models and the frozen specification determine DDL.
"""

from django.db.migrations.operations.base import Operation

MECHANICAL = {"sysid", "created_at", "updated_at", "created_by", "updated_by", "deleted_by", "revision"}
POLICY_NAMES = (
    "skeleton_tenant",
    "skeleton_membership_select",
    "skeleton_membership_insert",
    "skeleton_membership_update",
    "skeleton_membership_delete",
    "skeleton_self_select",
    "skeleton_self_insert",
    "skeleton_self_update",
    "skeleton_self_delete",
    "skeleton_audit_select",
    "skeleton_audit_insert",
)
TENANT_PREDICATE = "organization_id = NULLIF(current_setting('skeleton.tenant_id',true),'')::uuid"


def self_discovery_mode(model, specification):
    """How a tenant table lets a person read their own rows of other tenants.

    ``("self", column)`` for a frozen ``self_discovery_column``; ``("membership",
    "user_id")`` for the historical Membership specification installed before
    the key existed; ``(None, None)`` otherwise. Writes always check only the tenant.
    """
    column = specification.get("self_discovery_column")
    if column is not None:
        columns = {field.column for field in model._meta.concrete_fields}
        if column not in columns or column == "organization_id":
            raise ValueError(f"{model._meta.label}: self_discovery_column must be a concrete user column.")
        return "self", column
    if model._meta.label_lower == "sk_tenancy.membership":
        return "membership", "user_id"
    return None, None


def self_discovery_policies(prefix, column):
    """(command, roles, USING, WITH CHECK) per policy, shared by the DDL and the schema check."""
    tenant = TENANT_PREDICATE
    user = f"{column} = NULLIF(current_setting('skeleton.user_id',true),'')::uuid"
    return {
        f"{prefix}_select": ("r", ["skeleton_runtime"], f"{tenant} OR {user}", None),
        f"{prefix}_insert": ("a", ["skeleton_runtime"], None, tenant),
        f"{prefix}_update": ("w", ["skeleton_runtime"], tenant, tenant),
        f"{prefix}_delete": ("d", ["skeleton_runtime"], tenant, None),
    }


SENSITIVE = {"password", "token", "token_digest", "secret", "private_key", "recovery_codes"}


def _lit(value):
    return "'" + str(value).replace("'", "''") + "'"


def infrastructure_names(table, *, operational=False):
    """Trigger names of a table; operational tables have the identity guard only (no audit)."""
    suffixes = ("guard",) if operational else ("guard", "audit")
    return tuple(table + "_sk_" + suffix for suffix in suffixes)


def require_postgresql(connection):
    """Every data table lives in PostgreSQL (#144); any other backend is refused explicitly."""
    if connection.vendor != "postgresql":
        from django.core.exceptions import ImproperlyConfigured

        raise ImproperlyConfigured(
            "Record infrastructure requires PostgreSQL: the data layer has no other backend "
            f"(got {connection.vendor}). The instance SQLite stores configuration only."
        )


def is_operational(specification):
    return specification.get("lifecycle") == "operational"


def build_infrastructure_specification(model, spec):
    """Return the frozen specification to paste into a new migration.

    Authoring aid only: the result is written once into a migration and never
    recomputed from current models or the editable catalog when DDL runs.
    Column order follows ``concrete_fields``; relations report the target type.
    """
    sensitive = set(spec.sensitive_fields)
    columns = []
    for field in model._meta.concrete_fields:
        kind = field.target_field.get_internal_type() if field.is_relation else field.get_internal_type()
        columns.append(
            {
                "name": field.column,
                "type": kind,
                "sensitive": field.name in sensitive or field.column in sensitive,
            }
        )
    specification = {"scope": spec.scope, "sensitive_fields": list(spec.sensitive_fields)}
    if getattr(model, "lifecycle", None) == "operational":
        specification["lifecycle"] = "operational"
    discovery = getattr(model, "self_discovery_field", None)
    if discovery:
        specification["self_discovery_column"] = model._meta.get_field(discovery).column
    immutable = tuple(getattr(model, "immutable_fields", ()))
    if immutable:
        specification["immutable_fields"] = list(immutable)
    specification["columns"] = columns
    return specification


class InstallRecordInfrastructure(Operation):
    reversible = True
    reduces_to_sql = True

    def __init__(self, app_label, model_name, specification, replaces=None):
        self.app_label = app_label
        self.model_name = model_name
        self.specification = specification
        # The frozen specification this operation supersedes; backwards reinstalls it.
        self.replaces = replaces

    def deconstruct(self):
        kwargs = {"app_label": self.app_label, "model_name": self.model_name, "specification": self.specification}
        if self.replaces is not None:
            kwargs["replaces"] = self.replaces
        return (self.__class__.__name__, [], kwargs)

    def state_forwards(self, app_label, state):
        pass

    def describe(self):
        return f"Install record security for {self.app_label}.{self.model_name}"

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        model = to_state.apps.get_model(self.app_label, self.model_name)
        if not self.allow_migrate_model(schema_editor.connection.alias, model):
            return
        require_postgresql(schema_editor.connection)
        self._postgres(schema_editor, model)

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        if self.replaces is not None:
            # Superseding operation: restore the previous frozen infrastructure, never drop it.
            InstallRecordInfrastructure(self.app_label, self.model_name, self.replaces).database_forwards(
                app_label, schema_editor, from_state, to_state
            )
            return
        require_postgresql(schema_editor.connection)
        model = from_state.apps.get_model(self.app_label, self.model_name)
        table, q = model._meta.db_table, schema_editor.quote_name
        for name in infrastructure_names(table):
            schema_editor.execute(f"DROP TRIGGER IF EXISTS {q(name)} ON skeleton.{q(table)}")
        for kind in ("guard", "audit"):
            schema_editor.execute(f"DROP FUNCTION IF EXISTS skeleton.{q(table + '_sk_' + kind + '_fn')}()")
        for name in POLICY_NAMES:
            schema_editor.execute(f"DROP POLICY IF EXISTS {q(name)} ON skeleton.{q(table)}")
        schema_editor.execute(f"ALTER TABLE skeleton.{q(table)} DISABLE ROW LEVEL SECURITY")
        for field in self._org_refs(model):
            schema_editor.execute(
                f"ALTER TABLE skeleton.{q(table)} DROP CONSTRAINT IF EXISTS {q(table + '_sk_org_' + field.column)}"
            )

    def operational(self):
        return is_operational(self.specification)

    def runtime_privileges(self, model):
        if self._append_only(model):
            return {"SELECT"}
        if self.operational():
            return {"SELECT", "INSERT", "UPDATE", "DELETE"}
        return {"SELECT", "INSERT", "UPDATE"}

    def policy_names(self, model):
        """RLS policies this frozen specification installs on ``model``."""
        if self._append_only(model):
            return ("skeleton_audit_select", "skeleton_audit_insert")
        if not any(f.name == "organization" for f in model._meta.fields):
            return ()
        mode, column = self_discovery_mode(model, self.specification)
        if mode is None:
            return ("skeleton_tenant",)
        return tuple(self_discovery_policies(f"skeleton_{mode}", column))

    def _append_only(self, model):
        return self.specification.get("append_only", model._meta.label_lower == "sk_audit.auditevent")

    def _org_refs(self, model):
        return sorted(
            (
                field
                for field in model._meta.concrete_fields
                if field.is_relation
                and field.name != "organization"
                and any(f.name == "organization" for f in field.related_model._meta.concrete_fields)
            ),
            key=lambda field: field.column,
        )

    def _immutable(self, model):
        names = {"sysid"} | set(self.specification.get("immutable_fields", getattr(model, "immutable_fields", ())))
        if any(f.name == "organization" for f in model._meta.fields):
            names.add("organization_id")
        if model._meta.label_lower == "sk_tenancy.organization":
            names.add("slug")
        if model._meta.label_lower == "sk_tenancy.membership":
            names.add("user_id")
        return sorted(names)

    def _audit_fields(self, model):
        sensitive = set(self.specification.get("sensitive_fields", ())) | SENSITIVE
        declared = self.specification.get("columns")
        fields = {field.column: field for field in model._meta.concrete_fields}
        if declared is not None:
            if {column["name"] for column in declared} != set(fields):
                raise ValueError(
                    f"{model._meta.label}: frozen infrastructure columns must exactly match the historical model."
                )
            return [
                (
                    column["name"],
                    column.get("sensitive", False)
                    or fields[column["name"]].name in sensitive
                    or column["name"] in sensitive,
                )
                for column in declared
                if fields[column["name"]].name not in MECHANICAL
            ]
        return [
            (field.column, field.name in sensitive or field.column in sensitive)
            for field in model._meta.concrete_fields
            if field.name not in MECHANICAL
        ]

    def _postgres(self, editor, model):
        q, table = editor.quote_name, model._meta.db_table
        if self.operational():
            for kind in ("guard", "audit"):
                editor.execute(f"DROP TRIGGER IF EXISTS {q(table + '_sk_' + kind)} ON skeleton.{q(table)}")
        for field in self._org_refs(model):
            constraint = q(table + "_sk_org_" + field.column)
            target = q(field.related_model._meta.db_table)
            editor.execute(f"ALTER TABLE skeleton.{q(table)} DROP CONSTRAINT IF EXISTS {constraint}")
            editor.execute(
                f"ALTER TABLE skeleton.{q(table)} ADD CONSTRAINT {constraint} FOREIGN KEY (organization_id,{q(field.column)}) REFERENCES skeleton.{target}(organization_id,sysid)"
            )
            editor.execute(
                f"CREATE INDEX IF NOT EXISTS {q(table + '_sk_idx_' + field.column)} ON skeleton.{q(table)} (organization_id,{q(field.column)})"
            )
        append = self._append_only(model)
        operational = self.operational()
        immutable = " OR ".join(f"NEW.{q(name)} IS DISTINCT FROM OLD.{q(name)}" for name in self._immutable(model))
        tenant_column = self.specification.get("tenant_column") if operational else None
        tenant_guc = "NULLIF(current_setting('skeleton.tenant_id',true),'')"
        operational_guard = (
            f"IF {immutable} THEN RAISE EXCEPTION 'immutable record identity' USING ERRCODE='23514'; END IF;"
        )
        if tenant_column:
            # Global operational table: an INSERT made inside a tenant must target that tenant.
            operational_guard = (
                f"IF TG_OP='INSERT' THEN IF {tenant_guc} IS NOT NULL AND NEW.{q(tenant_column)} IS DISTINCT FROM {tenant_guc}::uuid "
                f"THEN RAISE EXCEPTION 'cross-tenant insert' USING ERRCODE='42501'; END IF; ELSE {operational_guard} END IF;"
            )
        guard = (
            operational_guard
            if operational
            else "IF TG_OP <> 'INSERT' THEN RAISE EXCEPTION 'audit is append only' USING ERRCODE='23514'; END IF; IF NEW.revision <> 1 OR NEW.deleted_at IS NOT NULL OR NEW.deleted_by IS NOT NULL THEN RAISE EXCEPTION 'invalid audit metadata' USING ERRCODE='23514'; END IF;"
            if append
            else f"IF TG_OP='DELETE' THEN RAISE EXCEPTION 'physical delete prohibited' USING ERRCODE='23514'; ELSIF TG_OP='INSERT' THEN IF NEW.revision <> 1 THEN RAISE EXCEPTION 'initial revision must be one' USING ERRCODE='23514'; END IF; ELSE IF {immutable} THEN RAISE EXCEPTION 'immutable record identity' USING ERRCODE='23514'; END IF; IF NEW.revision <> OLD.revision+1 THEN RAISE EXCEPTION 'revision must increment' USING ERRCODE='23514'; END IF; END IF;"
        )
        function = q(table + "_sk_guard_fn")
        editor.execute(
            f"CREATE OR REPLACE FUNCTION skeleton.{function}() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,skeleton AS $guard$ BEGIN {guard} RETURN NEW; END $guard$"
        )
        editor.execute(f"DROP TRIGGER IF EXISTS {q(table + '_sk_guard')} ON skeleton.{q(table)}")
        events = ("INSERT OR UPDATE" if tenant_column else "UPDATE") if operational else "INSERT OR UPDATE OR DELETE"
        editor.execute(
            f"CREATE TRIGGER {q(table + '_sk_guard')} BEFORE {events} ON skeleton.{q(table)} FOR EACH ROW EXECUTE FUNCTION skeleton.{function}()"
        )
        editor.execute(f"REVOKE ALL ON FUNCTION skeleton.{function}() FROM PUBLIC")
        tenant = TENANT_PREDICATE
        if append or any(f.name == "organization" for f in model._meta.fields):
            editor.execute(f"ALTER TABLE skeleton.{q(table)} ENABLE ROW LEVEL SECURITY")
            editor.execute(f"ALTER TABLE skeleton.{q(table)} FORCE ROW LEVEL SECURITY")
            for name in POLICY_NAMES:
                editor.execute(f"DROP POLICY IF EXISTS {q(name)} ON skeleton.{q(table)}")
            mode, column = (None, None) if append else self_discovery_mode(model, self.specification)
            if append:
                editor.execute(
                    f"CREATE POLICY skeleton_audit_select ON skeleton.{q(table)} FOR SELECT TO skeleton_runtime USING ({tenant} OR (organization_id IS NULL AND (current_setting('skeleton.platform_admin',true)='on' OR current_setting('skeleton.platform_auditor',true)='on')))"
                )
                editor.execute(
                    f"CREATE POLICY skeleton_audit_insert ON skeleton.{q(table)} FOR INSERT TO skeleton_audit_writer WITH CHECK ({tenant} OR organization_id IS NULL)"
                )
            elif mode is not None:
                # Self-discovery: read own rows of other tenants; every write checks only the tenant.
                commands = {"r": "SELECT", "a": "INSERT", "w": "UPDATE", "d": "DELETE"}
                for name, (command, _, using, check) in self_discovery_policies(f"skeleton_{mode}", column).items():
                    clauses = []
                    if using:
                        clauses.append(f"USING ({using})")
                    if check:
                        clauses.append(f"WITH CHECK ({check})")
                    editor.execute(
                        f"CREATE POLICY {name} ON skeleton.{q(table)} FOR {commands[command]} TO skeleton_runtime "
                        + " ".join(clauses)
                    )
            else:
                editor.execute(
                    f"CREATE POLICY skeleton_tenant ON skeleton.{q(table)} FOR ALL TO skeleton_runtime USING ({tenant}) WITH CHECK ({tenant})"
                )
        editor.execute(f"REVOKE ALL ON skeleton.{q(table)} FROM PUBLIC, skeleton_runtime")
        privileges = "SELECT" if append else "SELECT,INSERT,UPDATE,DELETE" if operational else "SELECT,INSERT,UPDATE"
        editor.execute(f"GRANT {privileges} ON skeleton.{q(table)} TO skeleton_runtime")
        if append:
            editor.execute(f"GRANT INSERT ON skeleton.{q(table)} TO skeleton_audit_writer")
            return
        if operational:
            editor.execute(f"DROP FUNCTION IF EXISTS skeleton.{q(table + '_sk_audit_fn')}()")
            return
        fields = self._audit_fields(model)
        build = []
        for column, sensitive in fields:
            old = "to_jsonb('[REDACTED]'::text)" if sensitive else f"old_data->{_lit(column)}"
            new = "to_jsonb('[REDACTED]'::text)" if sensitive else f"new_data->{_lit(column)}"
            build.append(
                f"IF TG_OP <> 'UPDATE' OR (old_data->{_lit(column)}) IS DISTINCT FROM (new_data->{_lit(column)}) THEN changes := changes || jsonb_build_object({_lit(column)},jsonb_build_object('old',CASE WHEN TG_OP='INSERT' THEN NULL ELSE {old} END,'new',CASE WHEN TG_OP='DELETE' THEN NULL ELSE {new} END,'redacted',{'true' if sensitive else 'false'})); END IF;"
            )
        org = (
            "(record_data->>'organization_id')::uuid"
            if any(f.name == "organization" for f in model._meta.fields)
            else "NULL"
        )
        function = q(table + "_sk_audit_fn")
        editor.execute(f"""CREATE OR REPLACE FUNCTION skeleton.{function}() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,skeleton AS $audit$
            DECLARE old_data jsonb; new_data jsonb; record_data jsonb; changes jsonb := '{{}}'::jsonb; operation text;
            BEGIN
            IF TG_OP <> 'INSERT' THEN old_data := to_jsonb(OLD); END IF;
            IF TG_OP <> 'DELETE' THEN new_data := to_jsonb(NEW); END IF;
            record_data := COALESCE(new_data,old_data);
            {" ".join(build)}
            IF TG_OP='UPDATE' AND changes='{{}}'::jsonb THEN RETURN NEW; END IF;
            operation := CASE WHEN TG_OP='INSERT' THEN 'create' WHEN TG_OP='DELETE' THEN 'hard_delete'
                WHEN old_data->>'deleted_at' IS NULL AND new_data->>'deleted_at' IS NOT NULL THEN 'soft_delete'
                WHEN old_data->>'deleted_at' IS NOT NULL AND new_data->>'deleted_at' IS NULL THEN 'restore' ELSE 'update' END;
            INSERT INTO skeleton.sk_audit_auditevent
            (sysid,created_at,updated_at,created_by,updated_by,deleted_by,deleted_at,revision,organization_id,table_name,model_label,record_sysid,record_revision,operation,actor_sysid,request_id,source,db_principal,changes)
            VALUES (md5(random()::text || clock_timestamp()::text)::uuid,clock_timestamp(),clock_timestamp(),
            NULLIF(current_setting('skeleton.user_id',true),'')::uuid,NULLIF(current_setting('skeleton.user_id',true),'')::uuid,NULL,NULL,1,
            {org},{_lit(table)},{_lit(model._meta.label_lower)},(record_data->>'sysid')::uuid,(record_data->>'revision')::bigint,operation,
            NULLIF(current_setting('skeleton.user_id',true),'')::uuid,NULLIF(current_setting('skeleton.request_id',true),'')::uuid,
            COALESCE(NULLIF(current_setting('skeleton.source',true),''),'sql:unattributed'),session_user,changes);
            RETURN COALESCE(NEW,OLD); END $audit$""")
        # Ownership transfer requires schema CREATE briefly; owner is an offline privileged role.
        editor.execute("GRANT CREATE ON SCHEMA skeleton TO skeleton_audit_writer")
        editor.execute(f"ALTER FUNCTION skeleton.{function}() OWNER TO skeleton_audit_writer")
        editor.execute("REVOKE CREATE ON SCHEMA skeleton FROM skeleton_audit_writer")
        editor.execute(f"REVOKE ALL ON FUNCTION skeleton.{function}() FROM PUBLIC, skeleton_runtime")
        editor.execute(f"DROP TRIGGER IF EXISTS {q(table + '_sk_audit')} ON skeleton.{q(table)}")
        editor.execute(
            f"CREATE TRIGGER {q(table + '_sk_audit')} AFTER INSERT OR UPDATE OR DELETE ON skeleton.{q(table)} FOR EACH ROW EXECUTE FUNCTION skeleton.{function}()"
        )
