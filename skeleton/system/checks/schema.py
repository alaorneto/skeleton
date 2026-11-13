"""check_deployed_schema: the installed schema against the frozen infrastructure (runtime role)."""

import re

from django.db import connections

from skeleton.common.migration_operations import infrastructure_names

from ..registry import all_specs
from ..synchronization import physical_tables, technical_manifest
from .encryption import stored_secrets_without_keys
from .frozen import Capture, _expected_sql, _frozen_operations
from .sqltext import _canonical, _sql_tokens, _trigger_tokens


def _check_lifecycle(cursor, operation, connection):
    capture = Capture(connection)
    operation.database_forwards("sk_accounts", capture, None, None)
    expected_body = capture.statements[0].split("$lifecycle$")[1]
    cursor.execute("""
        SELECT p.oid, r.rolname, p.prosecdef, p.proconfig, p.prosrc,
               oidvectortypes(p.proargtypes), p.prorettype::regtype::text, l.lanname, p.prokind,
               has_function_privilege('skeleton_runtime',p.oid,'EXECUTE'),
               has_function_privilege('skeleton_runtime',p.oid,'EXECUTE WITH GRANT OPTION')
        FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        JOIN pg_roles r ON r.oid=p.proowner JOIN pg_language l ON l.oid=p.prolang
        WHERE n.nspname='skeleton' AND p.proname='assert_user_deactivation'
    """)
    functions = cursor.fetchall()
    valid = len(functions) == 1
    if valid:
        function = functions[0]
        config = [item.replace(" ", "") for item in function[3] or []]
        valid = (
            function[1:3] == ("skeleton_owner", True)
            and config == ["search_path=pg_catalog,skeleton"]
            and _sql_tokens(function[4]) == _sql_tokens(expected_body)
            and function[5:] == ("uuid", "void", "plpgsql", "f", True, False)
        )
        cursor.execute(
            """
            SELECT a.grantee, a.privilege_type, a.is_grantable, r.rolname
            FROM pg_proc p CROSS JOIN LATERAL aclexplode(COALESCE(p.proacl,acldefault('f',p.proowner))) a
            LEFT JOIN pg_roles r ON r.oid=a.grantee WHERE p.oid=%s
        """,
            [function[0]],
        )
        grants = cursor.fetchall()
        valid = valid and all(
            role in {"skeleton_owner", "skeleton_runtime"}
            and privilege == "EXECUTE"
            and (role == "skeleton_owner" or not grantable)
            for grantee, privilege, grantable, role in grants
        )
    return [] if valid else ["assert_user_deactivation: exact lifecycle function inventory mismatch."]


def expected_policies(model, operation=None):
    from skeleton.common.migration_operations import TENANT_PREDICATE, self_discovery_mode, self_discovery_policies

    tenant = TENANT_PREDICATE
    if model._meta.label_lower == "sk_audit.auditevent":
        return {
            "skeleton_audit_select": (
                "r",
                ["skeleton_runtime"],
                tenant
                + " OR (organization_id IS NULL AND (current_setting('skeleton.platform_admin',true)='on' OR current_setting('skeleton.platform_auditor',true)='on'))",
                None,
            ),
            "skeleton_audit_insert": ("a", ["skeleton_audit_writer"], None, tenant + " OR organization_id IS NULL"),
        }
    if not any(field.name == "organization" for field in model._meta.fields):
        return {}
    mode, column = self_discovery_mode(model, operation.specification if operation is not None else {})
    if mode is not None:
        return self_discovery_policies(f"skeleton_{mode}", column)
    return {"skeleton_tenant": ("*", ["skeleton_runtime"], tenant, tenant)}


def _catalog_state(cursor):
    """Grants, effective privileges, sysid columns, trigger functions and tenant FKs of the schema.

    One query each, whatever the number of tables (#252): the checks below compare against
    these maps exactly what they used to query table by table.
    """
    cursor.execute(
        "SELECT table_name,privilege_type,is_grantable FROM information_schema.role_table_grants WHERE table_schema='skeleton' AND grantee='skeleton_runtime'"
    )
    grants = {}
    for table, privilege, grantable in cursor.fetchall():
        grants.setdefault(table, []).append((privilege, grantable))
    cursor.execute(
        "SELECT c.relname,a.action,has_table_privilege('skeleton_runtime',c.oid,a.action) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace CROSS JOIN unnest(ARRAY['SELECT','INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER']) AS a(action) WHERE n.nspname='skeleton' AND c.relkind IN ('r','p')"
    )
    effective = {(table, action): granted for table, action, granted in cursor.fetchall()}
    # Column privileges (#299): true with the privilege on the table or on any of its columns.
    cursor.execute(
        "SELECT c.relname,a.action,has_any_column_privilege('skeleton_runtime',c.oid,a.action) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace CROSS JOIN unnest(ARRAY['SELECT','INSERT','UPDATE','REFERENCES']) AS a(action) WHERE n.nspname='skeleton' AND c.relkind IN ('r','p')"
    )
    columns = {(table, action): granted for table, action, granted in cursor.fetchall()}
    cursor.execute(
        "SELECT c.relname,a.attname,t.typname FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_type t ON t.oid=a.atttypid WHERE n.nspname='skeleton' AND a.attname='sysid'"
    )
    sysid = {row[0]: row[1:] for row in cursor.fetchall()}
    cursor.execute(
        "SELECT p.proname,r.rolname,p.prosecdef,p.proconfig,p.prosrc,has_function_privilege('skeleton_runtime',p.oid,'EXECUTE') FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace JOIN pg_roles r ON r.oid=p.proowner WHERE n.nspname='skeleton' AND p.prokind='f' AND p.pronargs=0 AND p.prorettype='trigger'::regtype"
    )
    functions = {}
    for row in cursor.fetchall():
        functions.setdefault(row[0], row[1:])  # The first row, as fetchone() returned it.
    cursor.execute(
        "SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='skeleton'"
    )
    function_names = {row[0] for row in cursor.fetchall()}
    cursor.execute(
        """
        SELECT local.relname, c.conname,
               ARRAY(SELECT a.attname FROM unnest(c.conkey) WITH ORDINALITY k(num,position)
                     JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=k.num ORDER BY k.position),
               tn.nspname, target.relname,
               ARRAY(SELECT a.attname FROM unnest(c.confkey) WITH ORDINALITY k(num,position)
                     JOIN pg_attribute a ON a.attrelid=c.confrelid AND a.attnum=k.num ORDER BY k.position),
               c.convalidated,c.condeferrable,c.condeferred,c.confupdtype,c.confdeltype,c.confmatchtype
        FROM pg_constraint c JOIN pg_class local ON local.oid=c.conrelid
        JOIN pg_namespace ln ON ln.oid=local.relnamespace
        JOIN pg_class target ON target.oid=c.confrelid JOIN pg_namespace tn ON tn.oid=target.relnamespace
        WHERE ln.nspname='skeleton' AND c.contype='f'
    """
    )
    foreign_keys = {}
    for row in cursor.fetchall():
        foreign_keys.setdefault((row[0], row[1]), row[2:])
    return {
        "grants": grants,
        "effective": effective,
        "columns": columns,
        "sysid": sysid,
        "functions": functions,
        "function_names": function_names,
        "foreign_keys": foreign_keys,
    }


RELATION_KINDS = {"v": "view", "m": "materialized view", "f": "foreign table"}
# Functions of the schema outside the per-table infrastructure: the user lifecycle guard and the
# search wrapper of #180 (verified by search_problems).
KNOWN_FUNCTIONS = frozenset({"assert_user_deactivation", "immutable_unaccent"})


def _unclassified_relations(cursor):
    """Views, materialized views and foreign tables have no place in the inventory (#299)."""
    cursor.execute(
        "SELECT c.relname, c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname='skeleton' AND c.relkind IN ('v','m','f') ORDER BY c.relname"
    )
    return [f"Unclassified {RELATION_KINDS[kind]}: {name}" for name, kind in cursor.fetchall()]


def _unclassified_functions(cursor, specs, operations):
    """A function outside the inventory that is SECURITY DEFINER or executable by the runtime (#299).

    Extension functions are left out; a harmless helper the runtime cannot reach is tolerated.
    """
    allowed = set(KNOWN_FUNCTIONS)
    for name, spec in specs.items():
        allowed.add(name + "_sk_guard_fn")
        operation = operations.get(spec.model._meta.label_lower)
        append = spec.model._meta.label_lower == "sk_audit.auditevent"
        if operation is not None and not (append or operation.operational()):
            allowed.add(name + "_sk_audit_fn")
    cursor.execute(
        "SELECT p.proname, p.prosecdef, has_function_privilege('skeleton_runtime', p.oid, 'EXECUTE') "
        "FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
        "WHERE n.nspname='skeleton' AND NOT EXISTS ("
        "  SELECT 1 FROM pg_depend d WHERE d.classid='pg_proc'::regclass AND d.objid=p.oid AND d.deptype='e')"
    )
    reachable = {
        name for name, definer, executable in cursor.fetchall() if name not in allowed and (definer or executable)
    }
    return [f"Unclassified function reachable by runtime: {name}" for name in sorted(reachable)]


def _technical_table_triggers(triggers, tables, technical):
    """A technical table carries no trigger: its integrity is the migration's, not a row guard's (#299)."""
    return [
        f"{name}: technical tables must not carry triggers ({trigger})."
        for name, trigger in sorted(key for key in triggers if key[0] in tables & set(technical))
    ]


def check_deployed_schema(*, using="default"):
    from ..models import TableDefinition

    connection = connections[using]
    if connection.vendor != "postgresql":
        return ["Unsupported backend: the data layer is PostgreSQL only (skeleton.E025)."]
    errors = []
    tables = set(physical_tables(connection))
    specs = {spec.model._meta.db_table: spec for spec in all_specs()}
    from ..retained_schema import load_retained_contracts

    try:
        retained_specs, retained_operations = load_retained_contracts()
        operations, technical_privileges, lifecycle = _frozen_operations()
    except (ValueError, OSError, KeyError, TypeError) as exc:
        return [f"Invalid frozen infrastructure snapshot: {type(exc).__name__}"]
    for spec in retained_specs:
        if spec.model._meta.db_table in specs:
            return ["Retained schema overlaps an installed model."]
        specs[spec.model._meta.db_table] = spec
    operations.update(retained_operations)
    technical = technical_manifest()
    unknown = tables - set(specs) - set(technical)
    errors.extend(f"Unclassified physical table: {name}" for name in sorted(unknown))
    if TableDefinition._meta.db_table in tables:
        errors.extend(stored_secrets_without_keys(using=using, tables=tables))
    from ..bundled import bundled_divergences
    from ..models import ComponentInstallation

    if ComponentInstallation._meta.db_table in tables:
        errors.extend(bundled_divergences(using=using))
    # Full-text search (#180): the unaccent function and each declared search index.
    from ..search import search_problems

    errors.extend(search_problems(using=using)[0])
    if TableDefinition._meta.db_table not in tables:
        errors.append("Catalog migrations are missing.")
    else:
        catalog = set(
            TableDefinition.all_objects.using(using)
            .filter(database_alias=using, available=True)
            .values_list("db_table", flat=True)
        )
        errors.extend(f"Physical table missing from synchronized catalog: {name}" for name in sorted(tables - catalog))
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT rolname,rolsuper,rolbypassrls,rolcreaterole,rolcreatedb,rolcanlogin FROM pg_roles WHERE rolname IN ('skeleton_owner','skeleton_runtime','skeleton_audit_writer')"
        )
        roles = {row[0]: row[1:] for row in cursor.fetchall()}
        if roles.get("skeleton_runtime") != (False, False, False, False, True):
            errors.append("Runtime role must be LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEROLE NOCREATEDB.")
        if roles.get("skeleton_audit_writer") != (False, False, False, False, False):
            errors.append("Audit writer role must be NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEROLE NOCREATEDB.")
        if roles.get("skeleton_owner") != (False, True, False, False, True):
            errors.append("Migration owner must be LOGIN NOSUPERUSER BYPASSRLS NOCREATEROLE NOCREATEDB.")
        cursor.execute(
            "SELECT pg_has_role('skeleton_runtime','skeleton_owner','MEMBER'),pg_has_role('skeleton_runtime','skeleton_audit_writer','MEMBER'),has_schema_privilege('skeleton_runtime','skeleton','CREATE'),has_schema_privilege('skeleton_runtime','public','CREATE')"
        )
        if any(cursor.fetchone()):
            errors.append("Runtime has privileged role reachability or schema CREATE privileges.")
        cursor.execute(
            "SELECT c.relname,c.relrowsecurity,c.relforcerowsecurity,r.rolname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_roles r ON r.oid=c.relowner WHERE n.nspname='skeleton' AND c.relkind='r'"
        )
        relations = {row[0]: row[1:] for row in cursor.fetchall()}
        cursor.execute(
            "SELECT c.relname,t.tgname,t.tgenabled,pg_get_triggerdef(t.oid),pn.nspname,p.proname FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_proc p ON p.oid=t.tgfoid JOIN pg_namespace pn ON pn.oid=p.pronamespace WHERE n.nspname='skeleton' AND NOT t.tgisinternal"
        )
        triggers = {(row[0], row[1]): row[2:] for row in cursor.fetchall()}
        cursor.execute(
            "SELECT c.relname,p.polname,p.polcmd,ARRAY(SELECT rolname FROM pg_roles WHERE oid=ANY(p.polroles) ORDER BY rolname),pg_get_expr(p.polqual,p.polrelid),pg_get_expr(p.polwithcheck,p.polrelid),p.polpermissive FROM pg_policy p JOIN pg_class c ON c.oid=p.polrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='skeleton'"
        )
        policies = {}
        for row in cursor.fetchall():
            policies.setdefault(row[0], {})[row[1]] = row[2:]
        catalog_state = _catalog_state(cursor)
        # Objects the inventory does not classify (#299).
        errors.extend(_unclassified_relations(cursor))
        errors.extend(_unclassified_functions(cursor, specs, operations))
        errors.extend(_technical_table_triggers(triggers, tables, technical))
        if lifecycle is None:
            errors.append("assert_user_deactivation: migration-owned lifecycle inventory missing.")
        else:
            errors.extend(_check_lifecycle(cursor, lifecycle, connection))
        for name in sorted(tables & set(technical)):
            expected = technical_privileges.get(name)
            if expected is None:
                errors.append(f"{name}: frozen technical privilege inventory missing.")
                continue
            grants = catalog_state["grants"].get(name, [])
            if {row[0] for row in grants} != expected or any(row[1] != "NO" for row in grants):
                errors.append(f"{name}: exact technical runtime grant inventory mismatch.")
            for action in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
                if catalog_state["effective"].get((name, action), False) != (action in expected):
                    errors.append(f"{name}: unexpected effective technical privilege {action}.")
                if (
                    action in ("SELECT", "INSERT", "UPDATE", "REFERENCES")
                    and action not in expected
                    and catalog_state["columns"].get((name, action))
                ):
                    errors.append(f"{name}: runtime has a column-level {action} privilege outside the inventory.")
        for name, spec in specs.items():
            if name not in tables:
                errors.append(f"Missing own table: {name}")
                continue
            model = spec.model
            operation = operations.get(model._meta.label_lower)
            if operation is None:
                errors.append(f"{name}: migration-owned infrastructure specification missing.")
                continue
            try:
                expected_sql = _expected_sql(operation, model, connection)
            except ValueError as exc:
                errors.append(str(exc))
                continue
            declared_discovery = getattr(model, "self_discovery_field", None)
            frozen_discovery = operation.specification.get("self_discovery_column")
            if (model._meta.get_field(declared_discovery).column if declared_discovery else None) != frozen_discovery:
                errors.append(f"{name}: self_discovery_field differs from the frozen self_discovery_column.")
            declared_sensitive = {column for column, sensitive in operation._audit_fields(model) if sensitive}
            for field_name in spec.sensitive_fields:
                if model._meta.get_field(field_name).column not in declared_sensitive:
                    errors.append(
                        f"{name}.{field_name}: sensitive registry field absent from frozen audit specification."
                    )
            append = model._meta.label_lower == "sk_audit.auditevent"
            operational = operation.operational()
            if operational != (getattr(model, "lifecycle", None) == "operational"):
                errors.append(f"{name}: model lifecycle differs from the frozen operational specification.")
            expected_triggers = set(infrastructure_names(name, operational=operational))
            if append:
                expected_triggers = {trigger for trigger in expected_triggers if "audit" not in trigger[len(name) :]}
            actual = {key[1] for key in triggers if key[0] == name}
            if actual != expected_triggers or any(triggers[(name, trigger)][0] != "O" for trigger in actual):
                errors.append(f"{name}: exact enabled trigger inventory mismatch.")
            for trigger in expected_triggers & actual:
                definition = next(
                    (
                        sql
                        for sql in expected_sql
                        if sql.startswith("CREATE TRIGGER") and _sql_tokens(sql)[2] == trigger
                    ),
                    "",
                )
                # pg_get_triggerdef may omit the function schema under search_path.
                actual_trigger = triggers[(name, trigger)]
                qualified = re.sub(
                    r"EXECUTE FUNCTION\s+[^\s(]+\(",
                    f"EXECUTE FUNCTION {actual_trigger[2]}.{actual_trigger[3]}(",
                    actual_trigger[1],
                )
                if _trigger_tokens(qualified) != _trigger_tokens(definition):
                    errors.append(f"{trigger}: exact guard/audit trigger definition mismatch.")
            expected = expected_policies(model, operation)
            actual_policies = policies.get(name, {})
            if set(actual_policies) != set(expected):
                errors.append(f"{name}: exact RLS policy inventory mismatch.")
            for policy, (command, role, qualifier, check) in expected.items():
                actual_policy = actual_policies.get(policy)
                if (
                    actual_policy is None
                    or actual_policy[0] != command
                    or actual_policy[1] != role
                    or _canonical(actual_policy[2]) != _canonical(qualifier)
                    or _canonical(actual_policy[3]) != _canonical(check)
                    or not actual_policy[4]
                ):
                    errors.append(f"{name}.{policy}: RLS expression/command/role mismatch.")
            relation = relations.get(name)
            if not relation or relation[:2] != (bool(expected), bool(expected)) or relation[2] != "skeleton_owner":
                errors.append(f"{name}: RLS/FORCE/ownership mismatch.")
            if catalog_state["sysid"].get(name) != ("sysid", "uuid"):
                errors.append(f"{name}: physical UUID sysid missing.")
            grants = catalog_state["grants"].get(name, [])
            privileges = {row[0] for row in grants}
            allowed = operation.runtime_privileges(model)
            if privileges != allowed or any(row[1] != "NO" for row in grants):
                errors.append(f"{name}: exact runtime table grant inventory mismatch.")
            for forbidden in {
                "SELECT",
                "INSERT",
                "UPDATE",
                "DELETE",
                "TRUNCATE",
                "REFERENCES",
                "TRIGGER",
            } - allowed:
                if catalog_state["effective"].get((name, forbidden), False):
                    errors.append(f"{name}: runtime unexpectedly has {forbidden}.")
                if forbidden in ("SELECT", "INSERT", "UPDATE", "REFERENCES") and catalog_state["columns"].get(
                    (name, forbidden)
                ):
                    errors.append(f"{name}: runtime has a column-level {forbidden} privilege outside the inventory.")
            for kind in ("guard", "audit") if not (append or operational) else ("guard",):
                function_name = name + "_sk_" + kind + "_fn"
                function = catalog_state["functions"].get(function_name)
                definition = next(
                    (
                        sql
                        for sql in expected_sql
                        if sql.startswith("CREATE OR REPLACE FUNCTION") and f'"{function_name}"()' in sql
                    ),
                    "",
                )
                delimiter = "$audit$" if kind == "audit" else "$guard$"
                expected_body = definition.split(delimiter)[1] if delimiter in definition else ""
                owner = "skeleton_audit_writer" if kind == "audit" else "skeleton_owner"
                config = [item.replace(" ", "") for item in function[2] or []] if function else []
                if (
                    not function
                    or function[0] != owner
                    or function[1] != (kind == "audit")
                    or config != ["search_path=pg_catalog,skeleton"]
                    or _sql_tokens(function[3]) != _sql_tokens(expected_body)
                    or function[4]
                ):
                    errors.append(
                        f"{name}: exact {kind} function body/owner/security/search_path/EXECUTE inventory mismatch."
                    )
            if operational:
                if name + "_sk_audit_fn" in catalog_state["function_names"]:
                    errors.append(f"{name}: operational tables must not carry a row audit function.")
            for field in operation._org_refs(model):
                constraint_name = name + "_sk_org_" + field.column
                # PostgreSQL keeps identifiers to 63 bytes; the former ``conname = %s`` compared the same way.
                stored_name = constraint_name.encode()[:63].decode(errors="ignore")
                actual_fk = catalog_state["foreign_keys"].get((name, stored_name))
                expected_fk = (
                    ["organization_id", field.column],
                    "skeleton",
                    field.related_model._meta.db_table,
                    ["organization_id", "sysid"],
                    True,
                    False,
                    False,
                    "a",
                    "a",
                    "s",
                )
                if actual_fk != expected_fk:
                    errors.append(f"{name}.{field.column}: exact composite tenant foreign key mismatch.")
    return errors
