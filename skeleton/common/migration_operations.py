"""Migration-owned guards, tenant policies and transactional row auditing.

Only historical migration models and the frozen specification determine DDL.
"""
import re
from django.db.migrations.operations.base import Operation

MECHANICAL = {"sysid", "created_at", "updated_at", "created_by", "updated_by", "deleted_by", "revision"}
SENSITIVE = {"password", "token", "token_digest", "secret", "private_key", "recovery_codes"}


def _lit(value):
    return "'" + str(value).replace("'", "''") + "'"


def infrastructure_names(table, vendor):
    suffixes = ("guard", "audit") if vendor == "postgresql" else ("guard_insert", "guard_update", "guard_delete", "audit_insert", "audit_update", "audit_delete")
    return tuple(table + "_sk_" + suffix for suffix in suffixes)


class InstallRecordInfrastructure(Operation):
    reversible = True
    reduces_to_sql = True

    def __init__(self, app_label, model_name, specification):
        self.app_label = app_label
        self.model_name = model_name
        self.specification = specification

    def deconstruct(self):
        return (self.__class__.__name__, [], {"app_label": self.app_label, "model_name": self.model_name, "specification": self.specification})

    def state_forwards(self, app_label, state):
        pass

    def describe(self):
        return f"Install record security for {self.app_label}.{self.model_name}"

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        model = to_state.apps.get_model(self.app_label, self.model_name)
        if not self.allow_migrate_model(schema_editor.connection.alias, model):
            return
        if schema_editor.connection.vendor == "sqlite":
            self._sqlite(schema_editor, model)
        elif schema_editor.connection.vendor == "postgresql":
            self._postgres(schema_editor, model)
        else:
            raise NotImplementedError("Record infrastructure requires SQLite or PostgreSQL.")

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        model = from_state.apps.get_model(self.app_label, self.model_name)
        table, q = model._meta.db_table, schema_editor.quote_name
        if schema_editor.connection.vendor == "sqlite":
            for name in infrastructure_names(table, "sqlite"):
                schema_editor.execute(f"DROP TRIGGER IF EXISTS {q(name)}")
        else:
            for name in infrastructure_names(table, "postgresql"):
                schema_editor.execute(f"DROP TRIGGER IF EXISTS {q(name)} ON skeleton.{q(table)}")
            for kind in ("guard", "audit"):
                schema_editor.execute(f"DROP FUNCTION IF EXISTS skeleton.{q(table + '_sk_' + kind + '_fn')}()")
            for name in ("skeleton_tenant", "skeleton_membership_select", "skeleton_membership_insert", "skeleton_membership_update", "skeleton_membership_delete", "skeleton_audit_select", "skeleton_audit_insert"):
                schema_editor.execute(f"DROP POLICY IF EXISTS {q(name)} ON skeleton.{q(table)}")
            schema_editor.execute(f"ALTER TABLE skeleton.{q(table)} DISABLE ROW LEVEL SECURITY")
            for field in self._org_refs(model):
                schema_editor.execute(f"ALTER TABLE skeleton.{q(table)} DROP CONSTRAINT IF EXISTS {q(table + '_sk_org_' + field.column)}")

    def _append_only(self, model):
        return self.specification.get("append_only", model._meta.label_lower == "sk_audit.auditevent")

    def _org_refs(self, model):
        return sorted(
            (field for field in model._meta.concrete_fields if field.is_relation and field.name != "organization"
             and any(f.name == "organization" for f in field.related_model._meta.concrete_fields)),
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
                raise ValueError(f"{model._meta.label}: frozen infrastructure columns must exactly match the historical model.")
            return [(column["name"], column.get("sensitive", False) or fields[column["name"]].name in sensitive or column["name"] in sensitive)
                    for column in declared if fields[column["name"]].name not in MECHANICAL]
        return [(field.column, field.name in sensitive or field.column in sensitive) for field in model._meta.concrete_fields
                if field.name not in MECHANICAL]

    def _sqlite_value(self, model, column, reference, quote):
        field = next(field for field in model._meta.concrete_fields if field.column == column)
        value = f"{reference}.{quote(column)}"
        kind = field.target_field.get_internal_type() if field.is_relation else field.get_internal_type()
        if kind == "UUIDField":
            return f"CASE WHEN {value} IS NULL THEN NULL ELSE substr({value},1,8)||'-'||substr({value},9,4)||'-'||substr({value},13,4)||'-'||substr({value},17,4)||'-'||substr({value},21,12) END"
        if kind == "JSONField":
            return f"json({value})"
        if kind == "BooleanField":
            return f"CASE WHEN {value} IS NULL THEN NULL ELSE json(CASE WHEN {value} THEN 'true' ELSE 'false' END) END"
        return value

    def _sqlite(self, editor, model):
        q, table = editor.quote_name, model._meta.db_table
        append = self._append_only(model)
        for name in infrastructure_names(table, "sqlite"):
            editor.execute(f"DROP TRIGGER IF EXISTS {q(name)}")
        relation_guards = []
        for field in self._org_refs(model):
            target = field.related_model._meta.db_table
            relation_guards.append(f"SELECT CASE WHEN NEW.{q(field.column)} IS NOT NULL AND NOT EXISTS (SELECT 1 FROM {q(target)} WHERE sysid=NEW.{q(field.column)} AND organization_id=NEW.organization_id) THEN RAISE(ABORT, 'cross-tenant reference') END;")
        common = "\n".join(relation_guards)
        insert_guard = (
            "SELECT CASE WHEN NEW.sysid IS NULL OR length(NEW.sysid) != 32 OR NEW.sysid GLOB '*[^0-9a-f]*' "
            "THEN RAISE(ABORT, 'sysid must be a canonical UUID') END; "
            "SELECT CASE WHEN NEW.revision != 1 THEN RAISE(ABORT, 'initial revision must be one') END;"
        )
        if append:
            insert_guard += " SELECT CASE WHEN NEW.deleted_at IS NOT NULL OR NEW.deleted_by IS NOT NULL THEN RAISE(ABORT, 'audit is append only') END;"
        editor.execute(f"CREATE TRIGGER {q(table + '_sk_guard_insert')} BEFORE INSERT ON {q(table)} BEGIN {insert_guard} {common} END")
        if append:
            update_guard = "SELECT RAISE(ABORT, 'audit is append only');"
        else:
            clauses = [f"SELECT CASE WHEN NEW.{q(name)} IS NOT OLD.{q(name)} THEN RAISE(ABORT, 'immutable record identity') END;" for name in self._immutable(model)]
            clauses.append("SELECT CASE WHEN NEW.revision != OLD.revision + 1 THEN RAISE(ABORT, 'revision must increment') END;")
            update_guard = " ".join(clauses) + common
        editor.execute(f"CREATE TRIGGER {q(table + '_sk_guard_update')} BEFORE UPDATE ON {q(table)} BEGIN {update_guard} END")
        editor.execute(f"CREATE TRIGGER {q(table + '_sk_guard_delete')} BEFORE DELETE ON {q(table)} BEGIN SELECT RAISE(ABORT, 'physical delete prohibited'); END")
        if append:
            return
        fields = self._audit_fields(model)
        for operation in ("insert", "update", "delete"):
            ref = "OLD" if operation == "delete" else "NEW"
            changed = " OR ".join(f"OLD.{q(column)} IS NOT NEW.{q(column)}" for column, _ in fields) or "0"
            fragments = []
            for column, sensitive in fields:
                old = "NULL" if operation == "insert" else (_lit("[REDACTED]") if sensitive else self._sqlite_value(model, column, "OLD", q))
                new = "NULL" if operation == "delete" else (_lit("[REDACTED]") if sensitive else self._sqlite_value(model, column, "NEW", q))
                redacted = _lit("true" if sensitive else "false")
                entry = f"json_object({_lit(column)}, json_object('old',{old},'new',{new},'redacted',json({redacted})))"
                if operation == "update":
                    entry = f"CASE WHEN OLD.{q(column)} IS NOT NEW.{q(column)} THEN {entry} ELSE '{{}}' END"
                fragments.append(entry)
            # RFC 7396 json_patch removes null keys recursively. Aggregate complete
            # change objects instead, preserving both old/new even when null.
            union = " UNION ALL ".join(f"SELECT key,value FROM json_each({fragment})" for fragment in fragments)
            changes = f"(SELECT json_group_object(key,json(value)) FROM ({union}))" if fragments else "'{}'"
            org = f"{ref}.organization_id" if any(f.name == "organization" for f in model._meta.fields) else "NULL"
            action = _lit("create" if operation == "insert" else "hard_delete" if operation == "delete" else "update")
            if operation == "update":
                action = "CASE WHEN OLD.deleted_at IS NULL AND NEW.deleted_at IS NOT NULL THEN 'soft_delete' WHEN OLD.deleted_at IS NOT NULL AND NEW.deleted_at IS NULL THEN 'restore' ELSE 'update' END"
            editor.execute(f"""CREATE TRIGGER {q(table + '_sk_audit_' + operation)} AFTER {operation.upper()} ON {q(table)}
                {('WHEN ' + changed) if operation == 'update' else ''} BEGIN
                INSERT INTO sk_audit_auditevent
                (sysid,created_at,updated_at,created_by,updated_by,deleted_by,deleted_at,revision,
                 organization_id,table_name,model_label,record_sysid,record_revision,operation,actor_sysid,request_id,source,db_principal,changes)
                VALUES (skeleton_uuid(),skeleton_now(),skeleton_now(),skeleton_actor(),skeleton_actor(),NULL,NULL,1,
                 {org},{_lit(table)},{_lit(model._meta.label_lower)},{ref}.sysid,{ref}.revision,{action},skeleton_actor(),skeleton_request_id(),skeleton_source(),'sqlite',{changes}); END""")

    def _postgres(self, editor, model):
        q, table = editor.quote_name, model._meta.db_table
        relation_guards = []
        for field in self._org_refs(model):
            constraint = q(table + "_sk_org_" + field.column)
            target = q(field.related_model._meta.db_table)
            editor.execute(f"ALTER TABLE skeleton.{q(table)} DROP CONSTRAINT IF EXISTS {constraint}")
            editor.execute(f"ALTER TABLE skeleton.{q(table)} ADD CONSTRAINT {constraint} FOREIGN KEY (organization_id,{q(field.column)}) REFERENCES skeleton.{target}(organization_id,sysid)")
            editor.execute(f"CREATE INDEX IF NOT EXISTS {q(table + '_sk_idx_' + field.column)} ON skeleton.{q(table)} (organization_id,{q(field.column)})")
        append = self._append_only(model)
        immutable = " OR ".join(f"NEW.{q(name)} IS DISTINCT FROM OLD.{q(name)}" for name in self._immutable(model))
        guard = "IF TG_OP <> 'INSERT' THEN RAISE EXCEPTION 'audit is append only' USING ERRCODE='23514'; END IF; IF NEW.revision <> 1 OR NEW.deleted_at IS NOT NULL OR NEW.deleted_by IS NOT NULL THEN RAISE EXCEPTION 'invalid audit metadata' USING ERRCODE='23514'; END IF;" if append else f"IF TG_OP='DELETE' THEN RAISE EXCEPTION 'physical delete prohibited' USING ERRCODE='23514'; ELSIF TG_OP='INSERT' THEN IF NEW.revision <> 1 THEN RAISE EXCEPTION 'initial revision must be one' USING ERRCODE='23514'; END IF; ELSE IF {immutable} THEN RAISE EXCEPTION 'immutable record identity' USING ERRCODE='23514'; END IF; IF NEW.revision <> OLD.revision+1 THEN RAISE EXCEPTION 'revision must increment' USING ERRCODE='23514'; END IF; END IF;"
        function = q(table + "_sk_guard_fn")
        editor.execute(f"CREATE OR REPLACE FUNCTION skeleton.{function}() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,skeleton AS $guard$ BEGIN {guard} RETURN NEW; END $guard$")
        editor.execute(f"DROP TRIGGER IF EXISTS {q(table + '_sk_guard')} ON skeleton.{q(table)}")
        editor.execute(f"CREATE TRIGGER {q(table + '_sk_guard')} BEFORE INSERT OR UPDATE OR DELETE ON skeleton.{q(table)} FOR EACH ROW EXECUTE FUNCTION skeleton.{function}()")
        editor.execute(f"REVOKE ALL ON FUNCTION skeleton.{function}() FROM PUBLIC")
        tenant = "organization_id = NULLIF(current_setting('skeleton.tenant_id',true),'')::uuid"
        if append or any(f.name == "organization" for f in model._meta.fields):
            editor.execute(f"ALTER TABLE skeleton.{q(table)} ENABLE ROW LEVEL SECURITY")
            editor.execute(f"ALTER TABLE skeleton.{q(table)} FORCE ROW LEVEL SECURITY")
            for name in ("skeleton_tenant", "skeleton_membership_select", "skeleton_membership_insert", "skeleton_membership_update", "skeleton_membership_delete", "skeleton_audit_select", "skeleton_audit_insert"):
                editor.execute(f"DROP POLICY IF EXISTS {q(name)} ON skeleton.{q(table)}")
            if append:
                editor.execute(f"CREATE POLICY skeleton_audit_select ON skeleton.{q(table)} FOR SELECT TO skeleton_runtime USING ({tenant} OR (organization_id IS NULL AND current_setting('skeleton.platform_admin',true)='on'))")
                editor.execute(f"CREATE POLICY skeleton_audit_insert ON skeleton.{q(table)} FOR INSERT TO skeleton_audit_writer WITH CHECK ({tenant} OR organization_id IS NULL)")
            elif model._meta.label_lower == "sk_tenancy.membership":
                user = "user_id = NULLIF(current_setting('skeleton.user_id',true),'')::uuid"
                for command, expression in (("select", f"USING ({tenant} OR {user})"), ("insert", f"WITH CHECK ({tenant})"), ("update", f"USING ({tenant}) WITH CHECK ({tenant})"), ("delete", f"USING ({tenant})")):
                    editor.execute(f"CREATE POLICY skeleton_membership_{command} ON skeleton.{q(table)} FOR {command.upper()} TO skeleton_runtime {expression}")
            else:
                editor.execute(f"CREATE POLICY skeleton_tenant ON skeleton.{q(table)} FOR ALL TO skeleton_runtime USING ({tenant}) WITH CHECK ({tenant})")
        editor.execute(f"REVOKE ALL ON skeleton.{q(table)} FROM PUBLIC, skeleton_runtime")
        editor.execute(f"GRANT {'SELECT' if append else 'SELECT,INSERT,UPDATE'} ON skeleton.{q(table)} TO skeleton_runtime")
        if append:
            editor.execute(f"GRANT INSERT ON skeleton.{q(table)} TO skeleton_audit_writer")
            return
        fields = self._audit_fields(model)
        build = []
        for column, sensitive in fields:
            old = "to_jsonb('[REDACTED]'::text)" if sensitive else f"old_data->{_lit(column)}"
            new = "to_jsonb('[REDACTED]'::text)" if sensitive else f"new_data->{_lit(column)}"
            build.append(f"IF TG_OP <> 'UPDATE' OR (old_data->{_lit(column)}) IS DISTINCT FROM (new_data->{_lit(column)}) THEN changes := changes || jsonb_build_object({_lit(column)},jsonb_build_object('old',CASE WHEN TG_OP='INSERT' THEN NULL ELSE {old} END,'new',CASE WHEN TG_OP='DELETE' THEN NULL ELSE {new} END,'redacted',{'true' if sensitive else 'false'})); END IF;")
        org = "(record_data->>'organization_id')::uuid" if any(f.name == "organization" for f in model._meta.fields) else "NULL"
        function = q(table + "_sk_audit_fn")
        editor.execute(f"""CREATE OR REPLACE FUNCTION skeleton.{function}() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,skeleton AS $audit$
            DECLARE old_data jsonb; new_data jsonb; record_data jsonb; changes jsonb := '{{}}'::jsonb; operation text;
            BEGIN
            IF TG_OP <> 'INSERT' THEN old_data := to_jsonb(OLD); END IF;
            IF TG_OP <> 'DELETE' THEN new_data := to_jsonb(NEW); END IF;
            record_data := COALESCE(new_data,old_data);
            {' '.join(build)}
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
        editor.execute(f"CREATE TRIGGER {q(table + '_sk_audit')} AFTER INSERT OR UPDATE OR DELETE ON skeleton.{q(table)} FOR EACH ROW EXECUTE FUNCTION skeleton.{function}()")
