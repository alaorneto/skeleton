import re

from django.apps import apps
from django.conf import settings
from django.core.checks import Error, Tags, Warning, register
from django.db import connections, models

from skeleton.common.lifecycle_operation import InstallUserLifecycleGuard
from skeleton.common.migration_operations import InstallRecordInfrastructure, infrastructure_names
from skeleton.common.technical_operations import GrantTechnicalTablePrivileges

from .app_registry import CORE_APP_LABELS, all_apps, dependency_problems, validate_documentation
from .presentation import validate_presentation
from .registry import all_specs
from .synchronization import physical_tables, technical_manifest


@register()
def record_contract_checks(app_configs=None, **kwargs):
    from skeleton.common.models import OperationalRecord, OrganizationRecord, SecuredRecord, SystemRecord

    errors = []
    specs = {spec.model._meta.label_lower: spec for spec in all_specs()}
    registry = kwargs.get("apps_registry", apps)
    own_labels = CORE_APP_LABELS | {manifest.app_label for manifest in all_apps()}
    for model in registry.get_models():
        if model._meta.proxy:
            continue
        own = model._meta.app_label in own_labels or issubclass(model, SystemRecord | OperationalRecord)
        if not own:
            continue
        if (
            not issubclass(model, SystemRecord | OperationalRecord)
            or not isinstance(model._meta.pk, models.UUIDField)
            or model._meta.pk.name != "sysid"
        ):
            errors.append(
                Error(f"{model._meta.label}: own models require SystemRecord and UUID sysid PK.", id="skeleton.E001")
            )
        if (
            issubclass(model, SystemRecord)
            and not issubclass(model, OrganizationRecord)
            and "organization" in {field.name for field in model._meta.fields}
        ):
            # The name turns on tenant filtering in querysets and tenant DDL in migrations.
            errors.append(
                Error(
                    f"{model._meta.label}: a SystemRecord cannot declare a field named organization; "
                    "use another name such as granted_organization.",
                    id="skeleton.E017",
                )
            )
        spec = specs.get(model._meta.label_lower)
        if spec is None:
            errors.append(Error(f"{model._meta.label}: missing TableSpec.", id="skeleton.E002"))
            continue
        concrete = {field.name for field in model._meta.concrete_fields}
        system_fields = {
            "sysid",
            "organization",
            "created_at",
            "updated_at",
            "created_by",
            "updated_by",
            "deleted_at",
            "deleted_by",
            "revision",
            "owner",
            "visibility",
            "owning_group",
        }
        if (
            set(spec.read_fields) | set(spec.write_fields) | set(spec.sensitive_fields)
        ) - concrete or "__all__" in spec.read_fields + spec.write_fields:
            errors.append(
                Error(f"{model._meta.label}: field allowlists require concrete model fields.", id="skeleton.E003")
            )
        if set(spec.write_fields) & system_fields:
            errors.append(
                Error(f"{model._meta.label}: system/access fields are not externally writable.", id="skeleton.E004")
            )
        if spec.expose_api and not issubclass(model, SecuredRecord):
            errors.append(Error(f"{model._meta.label}: generic API requires SecuredRecord.", id="skeleton.E005"))
        if issubclass(model, OperationalRecord) and (spec.expose_api or spec.write_fields):
            errors.append(
                Error(
                    f"{model._meta.label}: operational tables are pipeline state; never exposed or externally writable.",
                    id="skeleton.E020",
                )
            )
        if issubclass(model, OrganizationRecord) and model._meta.get_field("organization").null:
            errors.append(Error(f"{model._meta.label}: tenant is mandatory.", id="skeleton.E006"))
        for field in model._meta.local_many_to_many:
            if field.remote_field.through._meta.auto_created:
                errors.append(
                    Error(
                        f"{model._meta.label}.{field.name}: own relations require explicit UUID through models.",
                        id="skeleton.E007",
                    )
                )
        for name in ("password", "token_digest", "secret", "private_key"):
            if name in concrete and name not in spec.sensitive_fields:
                errors.append(Error(f"{model._meta.label}.{name}: missing sensitive declaration.", id="skeleton.E008"))
        from django.db import models as django_models

        for name in getattr(spec, "search_fields", ()) or ():
            field = model._meta.get_field(name) if name in concrete else None
            if (
                field is None
                or name not in spec.read_fields
                or name in spec.sensitive_fields
                or not isinstance(field, django_models.CharField | django_models.TextField)
            ):
                errors.append(
                    Error(
                        f"{model._meta.label}.{name}: search_fields must be readable, non-sensitive text fields.",
                        id="skeleton.E031",
                    )
                )
    middleware = list(settings.MIDDLEWARE)
    from django.core.exceptions import ValidationError

    for problem in dependency_problems():
        errors.append(Error(f"Application dependency problem: {problem}.", id="skeleton.E011"))
    for manifest in all_apps():
        try:
            validate_documentation(manifest)
        except ValidationError as exc:
            errors.append(
                Error(
                    f"{manifest.app_label}: missing application spec/ADR/service API contract "
                    f"({'; '.join(exc.messages)}).",
                    id="skeleton.E012",
                )
            )
        try:
            validate_presentation(manifest)
        except ValidationError as exc:
            errors.append(Error(f"{manifest.app_label}: {'; '.join(exc.messages)}", id="skeleton.E013"))
    tenant = "skeleton.tenancy.middleware.TenantContextMiddleware"
    auth = "django.contrib.auth.middleware.AuthenticationMiddleware"
    account = "allauth.account.middleware.AccountMiddleware"
    messages = "django.contrib.messages.middleware.MessageMiddleware"
    token = "skeleton.integrations.tokens.middleware.ApiTokenMiddleware"
    setup = getattr(settings, "SKELETON_SETUP_MODE", False)  # Setup mode has no tenant at all (#146).
    if not setup and (
        tenant not in middleware
        or any(item not in middleware or middleware.index(item) > middleware.index(tenant) for item in (auth, messages))
        or (account in middleware and middleware.index(account) < middleware.index(tenant))
        # Bearer tokens (#46) replace request.user after authentication and before the tenant context.
        or (token in middleware and not middleware.index(auth) < middleware.index(token) < middleware.index(tenant))
    ):
        errors.append(
            Error("Tenant middleware must follow authentication/messages and precede allauth.", id="skeleton.E009")
        )
    if any(db.get("ATOMIC_REQUESTS") for db in settings.DATABASES.values()):
        errors.append(Error("ATOMIC_REQUESTS must be False; tenant middleware owns transactions.", id="skeleton.E010"))
    return errors


POSTGRESQL_ENGINE = "django.db.backends.postgresql"


@register()
def postgresql_only_checks(app_configs=None, **kwargs):
    """skeleton.E025: every DATABASES alias is PostgreSQL (#144); data never lives in SQLite.

    The instance configuration store is a separate SQLite file read before Django, never an alias.
    """
    from django.conf import settings

    dummy = "django.db.backends.dummy"  # What Django makes of an empty DATABASES.
    setup = getattr(settings, "SKELETON_SETUP_MODE", False)
    errors = [
        Error(
            f"DATABASES[{alias!r}] uses {config.get('ENGINE', '')!r}: the data layer is PostgreSQL only.",
            hint="Point DATABASE_URL at PostgreSQL (postgres://...); SQLite is only the instance configuration store.",
            id="skeleton.E025",
        )
        for alias, config in settings.DATABASES.items()
        if config.get("ENGINE") not in (POSTGRESQL_ENGINE, dummy)
    ]
    configured = [config for config in settings.DATABASES.values() if config.get("ENGINE") != dummy]
    if not configured and not setup:
        errors.append(
            Error(
                "No database is configured: DATABASE_URL (PostgreSQL) is required outside the setup.",
                hint="Run the first-access setup, `manage.py instance set database_url ...` or set DATABASE_URL.",
                id="skeleton.E025",
            )
        )
    return errors


@register()
def service_api_checks(app_configs=None, **kwargs):
    """E018: every registered app publishes a well-formed service API (docs/service-api.md)."""
    from skeleton.service_api.contract import structural_problems
    from skeleton.service_api.registry import DeclaredService, ServiceDeclarationError, resolve_classes

    errors = []
    for manifest in all_apps():
        label = manifest.app_label
        if manifest.service_api is None:
            # Two-step obligation: historical signed manifests keep loading until the core that
            # makes runtime.service_api mandatory; new releases are refused by build and doctor.
            errors.append(
                Warning(
                    f"{label}: the manifest does not declare runtime.service_api; publish service_api.py and "
                    "docs/service-api.md before the next release.",
                    id="skeleton.W018",
                )
            )
            continue
        declaration = DeclaredService(
            label, manifest.app_id or label, manifest.service_api.module, manifest.service_api.classes
        )
        try:
            app_module = apps.get_app_config(label).name
        except LookupError:
            app_module = None
        if app_module and not (declaration.module == app_module or declaration.module.startswith(app_module + ".")):
            errors.append(
                Error(
                    f"{label}: service API module {declaration.module} is outside the app package.", id="skeleton.E018"
                )
            )
            continue
        try:
            classes = resolve_classes(declaration)
        except ServiceDeclarationError as exc:
            errors.append(Error(f"{label}: {exc}", id="skeleton.E018"))
            continue
        for name, service_class in classes.items():
            if getattr(service_class, "__module__", None) != declaration.module and not str(
                getattr(service_class, "__module__", "")
            ).startswith(declaration.module + "."):
                errors.append(Error(f"{label}: {name} must be defined in {declaration.module}.", id="skeleton.E018"))
                continue
            problems = structural_problems(service_class)
            if not problems and service_class.app_label != label:
                # app_label makes every method require the provider app enabled in the organization.
                problems = [f"app_label must be {label!r} so calls require the app enabled (Unavailable)."]
            for problem in problems:
                errors.append(Error(f"{label}: {name}: {problem}", id="skeleton.E018"))
    return errors


def _boundary_providers(manifests):
    from .import_boundary import Provider

    providers = {}
    for label, manifest in manifests.items():
        try:
            app_module = apps.get_app_config(label).name
        except LookupError:
            continue
        packages = {app_module}
        if manifest.package_name:
            packages.add(f"applications.{manifest.package_name}")
        services = set()
        if manifest.service_api is not None:
            module = manifest.service_api.module
            services.add(module)
            for package in packages:
                if module.startswith(app_module + "."):
                    services.add(package + module[len(app_module) :])
        providers[label] = Provider(
            identity=label, packages=tuple(sorted(packages)), service_modules=tuple(sorted(services))
        )
    return providers


@register()
def import_boundary_checks(app_configs=None, **kwargs):
    """E019/W019: apps import other apps only through their declared service_api (docs/service-api.md)."""
    from .app_registry import get_app
    from .import_boundary import scan_application

    manifests = {manifest.app_label: manifest for manifest in all_apps()}
    providers = _boundary_providers(manifests)
    errors = []
    for label in sorted(manifests):
        if label not in providers:
            continue
        config = apps.get_app_config(label)
        own = providers[label]
        # get_app() resolves dependency app IDs to registered labels.
        dependencies = set(get_app(label).dependencies)
        violations = scan_application(
            config.path,
            package=config.name,
            aliases=[package for package in own.packages if package != config.name],
            providers=[provider for other, provider in providers.items() if other != label],
            dependencies=dependencies,
        )
        for violation in violations:
            message = f"{label}: {violation.path}:{violation.line}: {violation.message}"
            if violation.is_error:
                errors.append(Error(message, id="skeleton.E019"))
            else:
                # Non-public core surfaces are a warning first, an error in a later core.
                errors.append(Warning(message, id="skeleton.W019"))
    return errors


def undeclared_migration_dependencies(migrations, registered, allowed):
    """Registered app labels the migrations depend on outside the allowed (declared) set."""
    return sorted(
        {
            dependency[0]
            for migration in migrations
            for dependency in migration.dependencies
            if dependency[0] in registered and dependency[0] not in allowed
        }
    )


def undeclared_relations(models_of_app, registered, allowed):
    """(model, field, target app) for relations to registered apps outside the allowed set."""
    return sorted(
        {
            (model._meta.label, field.name, field.related_model._meta.app_label)
            for model in models_of_app
            for field in model._meta.concrete_fields
            if field.is_relation
            and isinstance(field.related_model, type)
            and field.related_model._meta.app_label in registered
            and field.related_model._meta.app_label not in allowed
        }
    )


@register()
def extension_consistency_checks(app_configs=None, **kwargs):
    """Database-free consistency between what an app declares and what its code does."""
    from django.db.migrations.loader import MigrationLoader

    from .app_registry import get_app, required_labels

    manifests = {manifest.app_label: manifest for manifest in all_apps()}
    if not manifests:
        return []
    errors = []
    loader = MigrationLoader(None, ignore_no_migrations=True)
    for label in sorted(manifests):
        allowed = {label, *required_labels(label)}
        migrations = [migration for key, migration in loader.disk_migrations.items() if key[0] == label]
        for other in undeclared_migration_dependencies(migrations, manifests, allowed):
            errors.append(
                Error(
                    f"{label}: migrations depend on {other}, which the manifest does not declare as a dependency.",
                    id="skeleton.E014",
                )
            )
        try:
            models_of_app = list(apps.get_app_config(label).get_models())
        except LookupError:
            models_of_app = []
        for model_label, field_name, other in undeclared_relations(models_of_app, manifests, allowed):
            errors.append(
                Error(
                    f"{model_label}.{field_name} references {other}, which the manifest does not declare as a dependency.",
                    id="skeleton.E015",
                )
            )
        canonical = getattr(get_app(label), "canonical", None)
        if canonical is not None:
            declared = set(canonical["migrations"]["leaves"])
            actual = {name for app_label, name in loader.graph.leaf_nodes(label) if app_label == label}
            if declared != actual:
                # A warning: authors run makemigrations before updating the manifest. build, doctor
                # and the deployment probe still refuse mismatched leaves.
                errors.append(
                    Warning(
                        f"{label}: manifest migrations.leaves {sorted(declared)} differ from the migration graph "
                        f"leaves {sorted(actual)}.",
                        id="skeleton.W017",
                    )
                )
    frozen, _, _ = _frozen_operations(from_migrations=True)
    for spec in all_specs():
        model = spec.model
        if model._meta.app_label not in manifests:
            continue
        operation = frozen.get(model._meta.label_lower)
        if operation is None:
            continue  # check_skeleton_schema reports missing infrastructure with database context.
        declared = set(operation.specification.get("sensitive_fields", ()))
        if declared != set(spec.sensitive_fields):
            errors.append(
                Error(
                    f"{model._meta.label}: TableSpec sensitive_fields {sorted(spec.sensitive_fields)} differ from the "
                    f"frozen migration specification {sorted(declared)}; add a migration with the new specification.",
                    id="skeleton.E016",
                )
            )
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
    return tuple(
        token
        for token in re.findall(r"'(?:''|[^'])*'|[a-zA-Z_][a-zA-Z_0-9]*|[^\s]", sql.replace('"', ""))
        if token != ";"
    )


def _trigger_tokens(sql):
    # PostgreSQL prints event order canonically, independent of the creation SQL.
    value = re.sub(
        r"\b(BEFORE|AFTER|INSTEAD OF)\s+(.+?)\s+ON\b",
        lambda match: match[1] + " " + " OR ".join(sorted(match[2].split(" OR "))) + " ON",
        sql,
    )
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
    operation._postgres(capture, model)
    return capture.statements


def expected_policies(model, operation=None):
    from skeleton.common.migration_operations import TENANT_PREDICATE, self_discovery_mode, self_discovery_policies

    tenant = TENANT_PREDICATE
    if model._meta.label_lower == "sk_audit.auditevent":
        return {
            "skeleton_audit_select": (
                "r",
                ["skeleton_runtime"],
                tenant + " OR (organization_id IS NULL AND current_setting('skeleton.platform_admin',true)='on')",
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


def check_deployed_schema(*, using="default"):
    from .models import TableDefinition

    connection = connections[using]
    if connection.vendor != "postgresql":
        return ["Unsupported backend: the data layer is PostgreSQL only (skeleton.E025)."]
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
    if TableDefinition._meta.db_table in tables:
        errors.extend(stored_secrets_without_keys(using=using, tables=tables))
    from .bundled import bundled_divergences
    from .models import ComponentInstallation

    if ComponentInstallation._meta.db_table in tables:
        errors.extend(bundled_divergences(using=using))
    # Full-text search (#180): the unaccent function and each declared search index.
    from .search import search_problems

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
        if lifecycle is None:
            errors.append("assert_user_deactivation: migration-owned lifecycle inventory missing.")
        else:
            errors.extend(_check_lifecycle(cursor, lifecycle, connection))
        for name in sorted(tables & set(technical)):
            expected = technical_privileges.get(name)
            if expected is None:
                errors.append(f"{name}: frozen technical privilege inventory missing.")
                continue
            cursor.execute(
                "SELECT privilege_type,is_grantable FROM information_schema.role_table_grants WHERE table_schema='skeleton' AND table_name=%s AND grantee='skeleton_runtime'",
                [name],
            )
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
            cursor.execute(
                "SELECT a.attname,t.typname FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_type t ON t.oid=a.atttypid WHERE n.nspname='skeleton' AND c.relname=%s AND a.attname='sysid'",
                [name],
            )
            if cursor.fetchone() != ("sysid", "uuid"):
                errors.append(f"{name}: physical UUID sysid missing.")
            cursor.execute(
                "SELECT privilege_type,is_grantable FROM information_schema.role_table_grants WHERE table_schema='skeleton' AND table_name=%s AND grantee='skeleton_runtime'",
                [name],
            )
            grants = cursor.fetchall()
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
                cursor.execute("SELECT has_table_privilege('skeleton_runtime',%s,%s)", ["skeleton." + name, forbidden])
                if cursor.fetchone()[0]:
                    errors.append(f"{name}: runtime unexpectedly has {forbidden}.")
            for kind in ("guard", "audit") if not (append or operational) else ("guard",):
                function_name = name + "_sk_" + kind + "_fn"
                cursor.execute(
                    "SELECT r.rolname,p.prosecdef,p.proconfig,p.prosrc,has_function_privilege('skeleton_runtime',p.oid,'EXECUTE') FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace JOIN pg_roles r ON r.oid=p.proowner WHERE n.nspname='skeleton' AND p.proname=%s AND p.prokind='f' AND p.pronargs=0 AND p.prorettype='trigger'::regtype",
                    [function_name],
                )
                function = cursor.fetchone()
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
                cursor.execute(
                    "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='skeleton' AND p.proname=%s",
                    [name + "_sk_audit_fn"],
                )
                if cursor.fetchone()[0]:
                    errors.append(f"{name}: operational tables must not carry a row audit function.")
            for field in operation._org_refs(model):
                constraint_name = name + "_sk_org_" + field.column
                cursor.execute(
                    """
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
                """,
                    [name, constraint_name],
                )
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
                if cursor.fetchone() != expected_fk:
                    errors.append(f"{name}.{field.column}: exact composite tenant foreign key mismatch.")
    return errors


KEYRINGS = (
    ("MFA_ENCRYPTION_KEYS", "MFA"),
    ("IDENTITY_PROVIDER_ENCRYPTION_KEYS", "identity provider"),
    ("INTEGRATION_ENCRYPTION_KEYS", "integration"),
)


@register()
def encrypted_secret_checks(app_configs=None, **kwargs):
    """Encrypted columns are audited as sensitive and configured keyrings are valid Fernet keys."""
    from django.core.exceptions import ImproperlyConfigured

    from skeleton.common.crypto import EncryptedTextField, validate_keyring
    from skeleton.system.registry import get_spec

    errors = []
    for setting, purpose in KEYRINGS:
        try:
            validate_keyring(setting, purpose=purpose)
        except ImproperlyConfigured as exc:
            errors.append(Error(str(exc), id="skeleton.E026"))
    for model in kwargs.get("apps_registry", apps).get_models():
        for field in model._meta.concrete_fields:
            if not isinstance(field, EncryptedTextField):
                continue
            try:
                sensitive = set(get_spec(model._meta.label_lower).sensitive_fields)
            except KeyError:
                continue  # E002 already reports the missing TableSpec.
            if field.name not in sensitive:
                errors.append(
                    Error(
                        f"{model._meta.label}.{field.name}: encrypted secrets must be in TableSpec.sensitive_fields.",
                        id="skeleton.E026",
                    )
                )
    return errors


def stored_secrets_without_keys(*, using="default", tables=None):
    """Messages for keyrings that are empty while encrypted values are stored (fails closed)."""
    from skeleton.common.crypto import encrypted_fields

    if tables is None:
        tables = set(physical_tables(connections[using]))
    problems = []
    for setting, _ in KEYRINGS:
        if getattr(settings, setting, ()):
            continue
        for model, field in encrypted_fields(setting):
            if model._meta.db_table not in tables:
                continue  # Not migrated yet: nothing stored.
            if model._base_manager.using(using).filter(**{f"{field.attname}__isnull": False}).exists():
                problems.append(f"{model._meta.label}.{field.name} stores secrets but {setting} is empty.")
    return problems


@register("database")
def stored_secret_key_checks(app_configs=None, databases=None, **kwargs):
    errors = []
    for alias in databases or ():
        errors.extend(Error(message, id="skeleton.E027") for message in stored_secrets_without_keys(using=alias))
    return errors


@register(Tags.security, deploy=True)
def shared_cache_checks(app_configs=None, **kwargs):
    """skeleton.W029: rate limits (#48) need a cache shared by every worker."""
    from django.conf import settings

    backend = settings.CACHES.get("default", {}).get("BACKEND", "")
    if not settings.DEBUG and backend.endswith(("locmem.LocMemCache", "dummy.DummyCache")):
        return [
            Warning(
                "The default cache is local to each process: token rate limits are per worker, not global.",
                hint="Set CACHE_URL to a shared Redis.",
                id="skeleton.W029",
            )
        ]
    return []
