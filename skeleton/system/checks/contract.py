"""Record contract checks of the TableSpecs and models (E001–E013, E017, E020) and identifier length (E033)."""

from django.apps import apps
from django.conf import settings
from django.core.checks import Error, register
from django.db import models

from ..app_registry import CORE_APP_LABELS, all_apps, dependency_problems, validate_documentation
from ..presentation import validate_presentation
from ..registry import all_specs


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
                Error(
                    f"{model._meta.label}: own models require SystemRecord and UUID sysid PK.",
                    hint="Derive the model from SystemRecord, OrganizationRecord, SecuredRecord or "
                    "OperationalRecord (skeleton.common.models) and do not declare another primary key. See "
                    "docs/checks.md.",
                    id="skeleton.E001",
                )
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
            errors.append(
                Error(
                    f"{model._meta.label}: missing TableSpec.",
                    hint="Register a TableSpec for the model in the app's AppConfig.ready() with "
                    "skeleton.system.registry.register(). See docs/extending.md and docs/checks.md.",
                    id="skeleton.E002",
                )
            )
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
                Error(
                    f"{model._meta.label}: field allowlists require concrete model fields.",
                    hint="List only concrete field names of this model in read_fields, write_fields and "
                    'sensitive_fields; "__all__" and related paths are not accepted. See docs/checks.md.',
                    id="skeleton.E003",
                )
            )
        if set(spec.write_fields) & system_fields:
            errors.append(
                Error(
                    f"{model._meta.label}: system/access fields are not externally writable.",
                    hint="Remove sysid, organization, created_*/updated_*/deleted_*, revision, owner, visibility "
                    "and owning_group from write_fields; access fields change only through the manage_access "
                    "service. See docs/checks.md.",
                    id="skeleton.E004",
                )
            )
        if spec.expose_api and not issubclass(model, SecuredRecord):
            errors.append(
                Error(
                    f"{model._meta.label}: generic API requires SecuredRecord.",
                    hint="Set expose_api=False in the TableSpec, or derive the model from SecuredRecord if it is "
                    "organization business data. See docs/checks.md.",
                    id="skeleton.E005",
                )
            )
        if issubclass(model, OperationalRecord) and (spec.expose_api or spec.write_fields):
            errors.append(
                Error(
                    f"{model._meta.label}: operational tables are pipeline state; never exposed or externally writable.",
                    id="skeleton.E020",
                )
            )
        if issubclass(model, OrganizationRecord) and model._meta.get_field("organization").null:
            errors.append(
                Error(
                    f"{model._meta.label}: tenant is mandatory.",
                    hint="Do not override the organization field of OrganizationRecord with null=True. See "
                    "docs/data-model.md and docs/checks.md.",
                    id="skeleton.E006",
                )
            )
        for field in model._meta.local_many_to_many:
            if field.remote_field.through._meta.auto_created:
                errors.append(
                    Error(
                        f"{model._meta.label}.{field.name}: own relations require explicit UUID through models.",
                        hint="Replace the implicit ManyToManyField with an explicit link model derived from "
                        "OrganizationRecord, with two PROTECT foreign keys. See docs/checks.md.",
                        id="skeleton.E007",
                    )
                )
        for name in ("password", "token_digest", "secret", "private_key"):
            if name in concrete and name not in spec.sensitive_fields:
                errors.append(
                    Error(
                        f"{model._meta.label}.{name}: missing sensitive declaration.",
                        hint="Add the field to TableSpec.sensitive_fields and install the record infrastructure again "
                        "in a new migration with the updated frozen specification. See docs/checks.md.",
                        id="skeleton.E008",
                    )
                )
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
            Error(
                "Tenant middleware must follow authentication/messages and precede allauth.",
                hint="In MIDDLEWARE, keep the order: AuthenticationMiddleware, MessageMiddleware, "
                "ApiTokenMiddleware (if present), TenantContextMiddleware, then allauth "
                "AccountMiddleware. Use the list in skeleton/conf/base.py. See docs/checks.md.",
                id="skeleton.E009",
            )
        )
    if any(db.get("ATOMIC_REQUESTS") for db in settings.DATABASES.values()):
        errors.append(
            Error(
                "ATOMIC_REQUESTS must be False; tenant middleware owns transactions.",
                hint="Remove ATOMIC_REQUESTS from every DATABASES entry; TenantContextMiddleware opens the "
                "request transaction. See docs/architecture.md and docs/checks.md.",
                id="skeleton.E010",
            )
        )
    return errors


@register()
def infrastructure_identifier_checks(app_configs=None, **kwargs):
    """skeleton.E033: no infrastructure identifier above PostgreSQL's 63 bytes (#238)."""
    from skeleton.common.migration_operations import MAX_IDENTIFIER_BYTES, oversized_identifiers
    from skeleton.common.models import OperationalRecord, SystemRecord

    errors = []
    for model in kwargs.get("apps_registry", apps).get_models():
        if model._meta.proxy or not issubclass(model, SystemRecord | OperationalRecord):
            continue
        for name, size in oversized_identifiers(model):
            errors.append(
                Error(
                    f"{model._meta.label}: infrastructure identifier {name} has {size} bytes; PostgreSQL "
                    f"truncates above {MAX_IDENTIFIER_BYTES}.",
                    hint="Shorten db_table or the db_column of the foreign key. See docs/checks.md.",
                    id="skeleton.E033",
                )
            )
    return errors
