import re
from django.apps import apps
from django.conf import settings
from django.core.checks import Error, register
from django.db import connections, models

from .registry import all_specs
from .app_registry import CORE_APP_LABELS, all_apps, validate_dependencies, validate_documentation
from .presentation import validate_presentation
from .synchronization import physical_tables, technical_manifest
from skeleton.common.migration_operations import infrastructure_names, InstallRecordInfrastructure
from skeleton.common.technical_operations import GrantTechnicalTablePrivileges
from skeleton.common.lifecycle_operation import InstallUserLifecycleGuard


@register()
def record_contract_checks(app_configs=None, **kwargs):
    from skeleton.common.models import SystemRecord, OrganizationRecord, SecuredRecord
    errors = []
    specs = {spec.model._meta.label_lower: spec for spec in all_specs()}
    registry = kwargs.get("apps_registry", apps)
    own_labels = CORE_APP_LABELS | {manifest.app_label for manifest in all_apps()}
    for model in registry.get_models():
        if model._meta.proxy:
            continue
        own = model._meta.app_label in own_labels or issubclass(model, SystemRecord)
        if not own:
            continue
        if not issubclass(model, SystemRecord) or not isinstance(model._meta.pk, models.UUIDField) or model._meta.pk.name != "sysid":
            errors.append(Error(f"{model._meta.label}: own models require SystemRecord and UUID sysid PK.", id="skeleton.E001"))
        spec = specs.get(model._meta.label_lower)
        if spec is None:
            errors.append(Error(f"{model._meta.label}: missing TableSpec.", id="skeleton.E002"))
            continue
        concrete = {field.name for field in model._meta.concrete_fields}
        system_fields = {"sysid", "organization", "created_at", "updated_at", "created_by", "updated_by", "deleted_at", "deleted_by", "revision", "owner", "visibility", "owning_group"}
        if (set(spec.read_fields) | set(spec.write_fields) | set(spec.sensitive_fields)) - concrete or "__all__" in spec.read_fields + spec.write_fields:
            errors.append(Error(f"{model._meta.label}: field allowlists require concrete model fields.", id="skeleton.E003"))
        if set(spec.write_fields) & system_fields:
            errors.append(Error(f"{model._meta.label}: system/access fields are not externally writable.", id="skeleton.E004"))
        if spec.expose_api and not issubclass(model, SecuredRecord):
            errors.append(Error(f"{model._meta.label}: generic API requires SecuredRecord.", id="skeleton.E005"))
        if issubclass(model, OrganizationRecord) and model._meta.get_field("organization").null:
            errors.append(Error(f"{model._meta.label}: tenant is mandatory.", id="skeleton.E006"))
        for field in model._meta.local_many_to_many:
            if field.remote_field.through._meta.auto_created:
                errors.append(Error(f"{model._meta.label}.{field.name}: own relations require explicit UUID through models.", id="skeleton.E007"))
        for name in ("password", "token_digest", "secret", "private_key"):
            if name in concrete and name not in spec.sensitive_fields:
                errors.append(Error(f"{model._meta.label}.{name}: missing sensitive declaration.", id="skeleton.E008"))
    middleware = list(settings.MIDDLEWARE)
    from django.core.exceptions import ValidationError
    try:
        validate_dependencies()
    except ValidationError:
        errors.append(Error("Application dependencies must be installed and acyclic.", id="skeleton.E011"))
    for manifest in all_apps():
        try:
            validate_documentation(manifest)
        except ValidationError:
            errors.append(Error(f"{manifest.app_label}: missing application spec/ADR contract.", id="skeleton.E012"))
        try:
            validate_presentation(manifest)
        except ValidationError as exc:
            errors.append(Error(f"{manifest.app_label}: {'; '.join(exc.messages)}", id="skeleton.E013"))
    tenant = "skeleton.tenancy.middleware.TenantContextMiddleware"
    auth = "django.contrib.auth.middleware.AuthenticationMiddleware"
    account = "allauth.account.middleware.AccountMiddleware"
    messages = "django.contrib.messages.middleware.MessageMiddleware"
    if tenant not in middleware or any(item not in middleware or middleware.index(item) > middleware.index(tenant) for item in (auth, messages)) or (account in middleware and middleware.index(account) < middleware.index(tenant)):
        errors.append(Error("Tenant middleware must follow authentication/messages and precede allauth.", id="skeleton.E009"))
    if any(db.get("ATOMIC_REQUESTS") for db in settings.DATABASES.values()):
        errors.append(Error("ATOMIC_REQUESTS must be False; tenant middleware owns transactions.", id="skeleton.E010"))
    return errors


def _canonical(expression):
    """Compare Boolean expression trees, retaining AND/OR grouping semantics."""
    if expression is None:
        return None
    value = re.sub(r"::(?:pg_catalog\.)?text\b", "", expression.lower())
    tokens = re.findall(r"'(?:''|[^'])*'|::|[a-z_][a-z_0-9.]*|[(),=]|[^\s]", value.replace('"', ""))

    def tree(items):
        while items and items[0] == "(" and items[-1] == ")":
            depth = 0
            enclosed = True
            for index, token in enumerate(items):
                depth += (token == "(") - (token == ")")
                if depth == 0 and index != len(items) - 1:
                    enclosed = False
                    break
            if not enclosed:
                break
            items = items[1:-1]
        for operator in ("or", "and"):
            depth, start, pieces = 0, 0, []
            for index, token in enumerate(items):
                depth += (token == "(") - (token == ")")
                if token == operator and depth == 0:
                    pieces.append(tree(items[start:index]))
                    start = index + 1
            if pieces:
                pieces.append(tree(items[start:]))
                return (operator, tuple(pieces))
        return tuple(token for token in items if token not in ("(", ")"))
    return tree(tokens)


def _sql_tokens(sql):
    return tuple(token for token in re.findall(r"'(?:''|[^'])*'|[a-zA-Z_][a-zA-Z_0-9]*|[^\s]", sql.replace('"', "")) if token != ";")


def _trigger_tokens(sql):
    # PostgreSQL prints event order canonically, independent of the creation SQL.
    value = re.sub(r"\b(BEFORE|AFTER|INSTEAD OF)\s+(.+?)\s+ON\b",
                   lambda match: match[1] + " " + " OR ".join(sorted(match[2].split(" OR "))) + " ON", sql)
    return _sql_tokens(value)


def _check_lifecycle(cursor, operation, connection):
    class Capture:
        def __init__(self):
            self.connection = connection
            self.statements = []
        def execute(self, sql):
            self.statements.append(sql)
    capture = Capture()
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
        valid = (function[1:3] == ("skeleton_owner", True)
                 and config == ["search_path=pg_catalog,skeleton"]
                 and _sql_tokens(function[4]) == _sql_tokens(expected_body)
                 and function[5:] == ("uuid", "void", "plpgsql", "f", True, False))
        cursor.execute("""
            SELECT a.grantee, a.privilege_type, a.is_grantable, r.rolname
            FROM pg_proc p CROSS JOIN LATERAL aclexplode(COALESCE(p.proacl,acldefault('f',p.proowner))) a
            LEFT JOIN pg_roles r ON r.oid=a.grantee WHERE p.oid=%s
        """, [function[0]])
        grants = cursor.fetchall()
        valid = valid and all(role in {"skeleton_owner", "skeleton_runtime"} and privilege == "EXECUTE"
                              and (role == "skeleton_owner" or not grantable)
                              for grantee, privilege, grantable, role in grants)
    return [] if valid else ["assert_user_deactivation: exact lifecycle function inventory mismatch."]


def _frozen_operations(*, from_migrations=False):
    reference = getattr(settings, "SKELETON_FROZEN_INFRASTRUCTURE", None)
    if reference and not from_migrations:
        from .infrastructure_snapshot import load_inventory
        return load_inventory(reference)
    from django.db.migrations.loader import MigrationLoader
    loader = MigrationLoader(None)
    result, technical, seen, lifecycle = {}, {}, set(), None
    for leaf in sorted(loader.graph.leaf_nodes()):
        for node in loader.graph.forwards_plan(leaf):
            if node in seen:
                continue
            seen.add(node)
            for operation in loader.disk_migrations[node].operations:
                if isinstance(operation, InstallRecordInfrastructure):
                    result[f"{operation.app_label}.{operation.model_name.lower()}"] = operation
                elif isinstance(operation, GrantTechnicalTablePrivileges):
                    technical.update({table: set(actions) for table, actions in operation.tables.items()})
                elif isinstance(operation, InstallUserLifecycleGuard):
                    lifecycle = operation
    return result, technical, lifecycle


def _expected_sql(operation, model, connection):
    class Capture:
        def __init__(self):
            self.connection = connection
            self.statements = []
        def quote_name(self, name):
            return connection.ops.quote_name(name)
        def execute(self, sql, params=None):
            self.statements.append(sql)
    capture = Capture()
    if connection.vendor == "sqlite":
        operation._sqlite(capture, model)
    else:
        operation._postgres(capture, model)
    return capture.statements


def expected_policies(model):
    tenant = "organization_id = NULLIF(current_setting('skeleton.tenant_id',true),'')::uuid"
    if model._meta.label_lower == "sk_audit.auditevent":
        return {"skeleton_audit_select": ("r", ["skeleton_runtime"], tenant + " OR (organization_id IS NULL AND current_setting('skeleton.platform_admin',true)='on')", None),
                "skeleton_audit_insert": ("a", ["skeleton_audit_writer"], None, tenant + " OR organization_id IS NULL")}
    if not any(field.name == "organization" for field in model._meta.fields):
        return {}
    if model._meta.label_lower == "sk_tenancy.membership":
        user = "user_id = NULLIF(current_setting('skeleton.user_id',true),'')::uuid"
        return {"skeleton_membership_select": ("r", ["skeleton_runtime"], tenant + " OR " + user, None),
                "skeleton_membership_insert": ("a", ["skeleton_runtime"], None, tenant),
                "skeleton_membership_update": ("w", ["skeleton_runtime"], tenant, tenant),
                "skeleton_membership_delete": ("d", ["skeleton_runtime"], tenant, None)}
    return {"skeleton_tenant": ("*", ["skeleton_runtime"], tenant, tenant)}


def check_deployed_schema(*, using="default"):
    from .models import TableDefinition
    from skeleton.common.models import OrganizationRecord
    connection = connections[using]
    if connection.vendor not in ("sqlite", "postgresql"):
        return ["Unsupported backend: only SQLite/PostgreSQL have record infrastructure."]
    errors = []
    tables = set(physical_tables(connection))
    specs = {spec.model._meta.db_table: spec for spec in all_specs()}
    from .retained_schema import load_retained_contracts
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
    if TableDefinition._meta.db_table not in tables:
        errors.append("Catalog migrations are missing.")
    else:
        catalog = set(TableDefinition.all_objects.using(using).filter(database_alias=using, available=True).values_list("db_table", flat=True))
        errors.extend(f"Physical table missing from synchronized catalog: {name}" for name in sorted(tables-catalog))
    with connection.cursor() as cursor:
        if connection.vendor == "sqlite":
            try:
                cursor.execute("SELECT skeleton_uuid(), skeleton_actor(), skeleton_request_id(), skeleton_source(), skeleton_now(), json_valid('{}')")
                if not cursor.fetchone()[-1]:
                    errors.append("SQLite JSON support is missing.")
            except Exception as exc:
                errors.append(f"SQLite audit functions unavailable: {type(exc).__name__}")
            cursor.execute("SELECT name, tbl_name, sql FROM sqlite_master WHERE type='trigger'")
            triggers = {row[0]: (row[1], row[2]) for row in cursor.fetchall()}
        else:
            cursor.execute("SELECT rolname,rolsuper,rolbypassrls,rolcreaterole,rolcreatedb,rolcanlogin FROM pg_roles WHERE rolname IN ('skeleton_owner','skeleton_runtime','skeleton_audit_writer')")
            roles = {row[0]: row[1:] for row in cursor.fetchall()}
            if roles.get("skeleton_runtime") != (False, False, False, False, True):
                errors.append("Runtime role must be LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEROLE NOCREATEDB.")
            if roles.get("skeleton_audit_writer") != (False, False, False, False, False):
                errors.append("Audit writer role must be NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEROLE NOCREATEDB.")
            if roles.get("skeleton_owner") != (False, True, False, False, True):
                errors.append("Migration owner must be LOGIN NOSUPERUSER BYPASSRLS NOCREATEROLE NOCREATEDB.")
            cursor.execute("SELECT pg_has_role('skeleton_runtime','skeleton_owner','MEMBER'),pg_has_role('skeleton_runtime','skeleton_audit_writer','MEMBER'),has_schema_privilege('skeleton_runtime','skeleton','CREATE'),has_schema_privilege('skeleton_runtime','public','CREATE')")
            if any(cursor.fetchone()):
                errors.append("Runtime has privileged role reachability or schema CREATE privileges.")
            cursor.execute("SELECT c.relname,c.relrowsecurity,c.relforcerowsecurity,r.rolname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_roles r ON r.oid=c.relowner WHERE n.nspname='skeleton' AND c.relkind='r'")
            relations = {row[0]: row[1:] for row in cursor.fetchall()}
            cursor.execute("SELECT c.relname,t.tgname,t.tgenabled,pg_get_triggerdef(t.oid),pn.nspname,p.proname FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_proc p ON p.oid=t.tgfoid JOIN pg_namespace pn ON pn.oid=p.pronamespace WHERE n.nspname='skeleton' AND NOT t.tgisinternal")
            triggers = {(row[0], row[1]): row[2:] for row in cursor.fetchall()}
            cursor.execute("SELECT c.relname,p.polname,p.polcmd,ARRAY(SELECT rolname FROM pg_roles WHERE oid=ANY(p.polroles) ORDER BY rolname),pg_get_expr(p.polqual,p.polrelid),pg_get_expr(p.polwithcheck,p.polrelid),p.polpermissive FROM pg_policy p JOIN pg_class c ON c.oid=p.polrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='skeleton'")
            policies = {}
            for row in cursor.fetchall():
                policies.setdefault(row[0], {})[row[1]] = row[2:]
            if lifecycle is None:
                errors.append("assert_user_deactivation: migration-owned lifecycle inventory missing.")
            else:
                errors.extend(_check_lifecycle(cursor, lifecycle, connection))
            for name in sorted(tables & set(technical)):
                expected = technical_privileges.get(name)
                if expected is None:
                    errors.append(f"{name}: frozen technical privilege inventory missing.")
                    continue
                cursor.execute("SELECT privilege_type,is_grantable FROM information_schema.role_table_grants WHERE table_schema='skeleton' AND table_name=%s AND grantee='skeleton_runtime'", [name])
                grants = cursor.fetchall()
                if {row[0] for row in grants} != expected or any(row[1] != "NO" for row in grants):
                    errors.append(f"{name}: exact technical runtime grant inventory mismatch.")
                for action in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
                    cursor.execute("SELECT has_table_privilege('skeleton_runtime',%s,%s)", ["skeleton." + name, action])
                    if cursor.fetchone()[0] != (action in expected):
                        errors.append(f"{name}: unexpected effective technical privilege {action}.")
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
            declared_sensitive = {column for column, sensitive in operation._audit_fields(model) if sensitive}
            for field_name in spec.sensitive_fields:
                if model._meta.get_field(field_name).column not in declared_sensitive:
                    errors.append(f"{name}.{field_name}: sensitive registry field absent from frozen audit specification.")
            append = model._meta.label_lower == "sk_audit.auditevent"
            expected_triggers = set(infrastructure_names(name, connection.vendor))
            if append:
                expected_triggers = {trigger for trigger in expected_triggers if "audit" not in trigger[len(name):]}
            if connection.vendor == "sqlite":
                actual = {key for key, (table, sql) in triggers.items() if table == name}
                if actual != expected_triggers:
                    errors.append(f"{name}: exact trigger inventory mismatch: expected {sorted(expected_triggers)}, got {sorted(actual)}")
                expected_bodies = {}
                for statement in expected_sql:
                    match = re.match(r'CREATE TRIGGER "([^"]+)"', statement)
                    if match:
                        expected_bodies[match.group(1)] = statement
                for trigger in expected_triggers & actual:
                    if _sql_tokens(triggers[trigger][1]) != _sql_tokens(expected_bodies.get(trigger, "")):
                        errors.append(f"{trigger}: exact guard/audit trigger definition mismatch.")
                for field in InstallRecordInfrastructure(model._meta.app_label, model._meta.model_name, {})._org_refs(model):
                    for suffix in ("guard_insert", "guard_update"):
                        sql = triggers.get(name + "_sk_" + suffix, (None, ""))[1]
                        if field.column not in sql or field.related_model._meta.db_table not in sql or "cross-tenant reference" not in sql:
                            errors.append(f"{name}.{field.column}: same-tenant guard missing.")
            else:
                actual = {key[1] for key in triggers if key[0] == name}
                if actual != expected_triggers or any(triggers[(name, trigger)][0] != "O" for trigger in actual):
                    errors.append(f"{name}: exact enabled trigger inventory mismatch.")
                for trigger in expected_triggers & actual:
                    definition = next((sql for sql in expected_sql if sql.startswith("CREATE TRIGGER")
                                       and _sql_tokens(sql)[2] == trigger), "")
                    # pg_get_triggerdef may omit the function schema under search_path.
                    actual_trigger = triggers[(name, trigger)]
                    qualified = re.sub(r"EXECUTE FUNCTION\s+[^\s(]+\(",
                                       f"EXECUTE FUNCTION {actual_trigger[2]}.{actual_trigger[3]}(", actual_trigger[1])
                    if _trigger_tokens(qualified) != _trigger_tokens(definition):
                        errors.append(f"{trigger}: exact guard/audit trigger definition mismatch.")
                expected = expected_policies(model)
                actual_policies = policies.get(name, {})
                if set(actual_policies) != set(expected):
                    errors.append(f"{name}: exact RLS policy inventory mismatch.")
                for policy, (command, role, qualifier, check) in expected.items():
                    actual_policy = actual_policies.get(policy)
                    if actual_policy is None or actual_policy[0] != command or actual_policy[1] != role or _canonical(actual_policy[2]) != _canonical(qualifier) or _canonical(actual_policy[3]) != _canonical(check) or not actual_policy[4]:
                        errors.append(f"{name}.{policy}: RLS expression/command/role mismatch.")
                relation = relations.get(name)
                if not relation or relation[:2] != (bool(expected), bool(expected)) or relation[2] != "skeleton_owner":
                    errors.append(f"{name}: RLS/FORCE/ownership mismatch.")
                cursor.execute("SELECT a.attname,t.typname FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_type t ON t.oid=a.atttypid WHERE n.nspname='skeleton' AND c.relname=%s AND a.attname='sysid'", [name])
                if cursor.fetchone() != ("sysid", "uuid"):
                    errors.append(f"{name}: physical UUID sysid missing.")
                cursor.execute("SELECT privilege_type,is_grantable FROM information_schema.role_table_grants WHERE table_schema='skeleton' AND table_name=%s AND grantee='skeleton_runtime'", [name])
                grants = cursor.fetchall()
                privileges = {row[0] for row in grants}
                if privileges != ({"SELECT"} if append else {"SELECT", "INSERT", "UPDATE"}) or any(row[1] != "NO" for row in grants):
                    errors.append(f"{name}: exact runtime table grant inventory mismatch.")
                for forbidden in ({"SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"} - ({"SELECT"} if append else {"SELECT", "INSERT", "UPDATE"})):
                    cursor.execute("SELECT has_table_privilege('skeleton_runtime',%s,%s)", ["skeleton." + name, forbidden])
                    if cursor.fetchone()[0]:
                        errors.append(f"{name}: runtime unexpectedly has {forbidden}.")
                for kind in ("guard", "audit") if not append else ("guard",):
                    function_name = name + "_sk_" + kind + "_fn"
                    cursor.execute("SELECT r.rolname,p.prosecdef,p.proconfig,p.prosrc,has_function_privilege('skeleton_runtime',p.oid,'EXECUTE') FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace JOIN pg_roles r ON r.oid=p.proowner WHERE n.nspname='skeleton' AND p.proname=%s AND p.prokind='f' AND p.pronargs=0 AND p.prorettype='trigger'::regtype", [function_name])
                    function = cursor.fetchone()
                    definition = next((sql for sql in expected_sql if sql.startswith("CREATE OR REPLACE FUNCTION") and f'"{function_name}"()' in sql), "")
                    delimiter = "$audit$" if kind == "audit" else "$guard$"
                    expected_body = definition.split(delimiter)[1] if delimiter in definition else ""
                    owner = "skeleton_audit_writer" if kind == "audit" else "skeleton_owner"
                    config = [item.replace(" ", "") for item in function[2] or []] if function else []
                    if not function or function[0] != owner or function[1] != (kind == "audit") or config != ["search_path=pg_catalog,skeleton"] or _sql_tokens(function[3]) != _sql_tokens(expected_body) or function[4]:
                        errors.append(f"{name}: exact {kind} function body/owner/security/search_path/EXECUTE inventory mismatch.")
                for field in operation._org_refs(model):
                    constraint_name = name + "_sk_org_" + field.column
                    cursor.execute("""
                        SELECT ARRAY(SELECT a.attname FROM unnest(c.conkey) WITH ORDINALITY k(num,position)
                                     JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=k.num ORDER BY k.position),
                               tn.nspname, target.relname,
                               ARRAY(SELECT a.attname FROM unnest(c.confkey) WITH ORDINALITY k(num,position)
                                     JOIN pg_attribute a ON a.attrelid=c.confrelid AND a.attnum=k.num ORDER BY k.position),
                               c.convalidated,c.condeferrable,c.condeferred,c.confupdtype,c.confdeltype,c.confmatchtype
                        FROM pg_constraint c JOIN pg_class local ON local.oid=c.conrelid
                        JOIN pg_namespace ln ON ln.oid=local.relnamespace
                        JOIN pg_class target ON target.oid=c.confrelid JOIN pg_namespace tn ON tn.oid=target.relnamespace
                        WHERE ln.nspname='skeleton' AND local.relname=%s AND c.conname=%s AND c.contype='f'
                    """, [name, constraint_name])
                    expected_fk = (["organization_id", field.column], "skeleton", field.related_model._meta.db_table,
                                   ["organization_id", "sysid"], True, False, False, "a", "a", "s")
                    if cursor.fetchone() != expected_fk:
                        errors.append(f"{name}.{field.column}: exact composite tenant foreign key mismatch.")
    return errors
