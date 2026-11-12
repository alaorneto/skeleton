"""OpenAPI 3.1 schema of ``/api/v1/`` (#178), generated from the code on every request.

Three sources, no hand-maintained document:

- the generic gateway (records, restore, access, references, attachments) comes from each
  exposed ``TableSpec``: ``read_fields`` and ``write_fields`` typed by ``_meta`` with
  :func:`skeleton.common.migration_operations.field_internal_type`. ``sensitive_fields`` never
  appear, not even by name, as properties, filters or reference pickers;
- the access administration (``DedicatedView``) comes from ``skeleton.api.serializers.DEDICATED``;
- every other API view declares ``openapi``: a mapping of HTTP method to :class:`Operation`.
  Views that serve a collection and a detail route mark operations with ``detail``.

The routes are the real URL tree (``get_resolver``), so views outside ``skeleton/api/urls.py``
(notifications, apps, webhooks, tokens, app API URLs) are described where they are mounted.
A view under ``/api/v1/`` without ``openapi`` still appears, with an open body and
``x-skeleton-undocumented: true``.

``GET /api/v1/openapi.json`` (``OpenAPIView``) describes only what the actor reaches
(:func:`audience_for`): it reads each of the actor's organizations in its own read-only context
(``for_each_organization``), so a table appears only with a TablePermission of the actor there,
an operation only with its capability, an app route only where the app is active, and a token
sees only the methods in ``token_methods`` and the actions of its scopes. Nothing from an
organization the actor does not belong to is read. :func:`full_audience` describes everything
for the contract tests (``skeleton.testing.openapi``); no HTTP request can select it.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

OPENAPI_VERSION = "3.1.0"
ORGANIZATION_ADMIN = "organization_admin"
TABLE_ACTIONS = ("read", "create", "update", "delete", "restore", "manage_access")
UNDOCUMENTED = "x-skeleton-undocumented"

Schema = dict[str, Any]
SchemaSource = Schema | Callable[[], Schema] | None


# --- JSON Schema builders ------------------------------------------------------------------


def string(**keywords: Any) -> Schema:
    return {"type": "string", **keywords}


UUID: Schema = {"type": "string", "format": "uuid"}
DATETIME: Schema = {"type": "string", "format": "date-time"}
INTEGER: Schema = {"type": "integer"}
BOOLEAN: Schema = {"type": "boolean"}
ANY: Schema = {}
OBJECT: Schema = {"type": "object"}


def nullable(schema: Schema) -> Schema:
    """The same schema accepting ``null``."""
    result = dict(schema)
    if "enum" in result and None not in result["enum"]:
        result["enum"] = [*result["enum"], None]
    kind = result.get("type")
    if isinstance(kind, str):
        result["type"] = [kind, "null"]
    elif isinstance(kind, list) and "null" not in kind:
        result["type"] = [*kind, "null"]
    elif "$ref" in result:
        return {"anyOf": [result, {"type": "null"}]}
    return result


def array(items: Schema) -> Schema:
    return {"type": "array", "items": items}


def obj(properties: Mapping[str, Schema], *, required: tuple[str, ...] | None = None, closed: bool = True) -> Schema:
    """An object; every property is required unless ``required`` says otherwise."""
    schema: Schema = {"type": "object", "properties": dict(properties)}
    names = tuple(properties) if required is None else required
    if names:
        schema["required"] = list(names)
    if closed:
        schema["additionalProperties"] = False
    return schema


def ref(name: str) -> Schema:
    return {"$ref": f"#/components/schemas/{name}"}


def results(items: Schema) -> Schema:
    """``{"results": [...]}`` of the unpaginated lists."""
    return obj({"results": array(items)})


# --- Operations declared by views ----------------------------------------------------------


@dataclass(frozen=True)
class Operation:
    """One documented operation of a view.

    ``response`` and ``request`` are JSON Schemas, or callables returning one (evaluated when the
    schema is built, so they may read ``_meta``). ``response=None`` means no body (``204``).
    ``paginated`` wraps ``response`` as the items of a page; ``etag`` documents the ``ETag``
    header; ``if_match`` the required ``If-Match`` with the ``412``/``428`` answers.
    ``capability`` (or :data:`ORGANIZATION_ADMIN`) and ``scope`` (a named token scope) hide the
    operation from an actor that lacks them. ``detail`` restricts it to routes with (``True``) or
    without (``False``) the view's ``openapi_detail`` parameter.
    """

    summary: str
    response: SchemaSource = None
    status: int | str = 200
    request: SchemaSource = None
    request_required: bool = True
    paginated: bool = False
    page_extra: Mapping[str, Schema] = field(default_factory=dict)
    etag: bool = False
    if_match: bool = False
    if_match_optional: bool = False
    detail: bool | None = None
    query: tuple[tuple[str, Schema, str], ...] = ()
    multipart: bool = False
    capability: str | None = None
    scope: str | None = None
    description: str = ""
    extra_responses: Mapping[str, str] = field(default_factory=dict)


def _resolve(source: SchemaSource) -> Schema | None:
    return source() if callable(source) else source


# --- Audience: what the actor reaches -------------------------------------------------------


@dataclass(frozen=True)
class Audience:
    """Union over the actor's organizations of what the schema may describe."""

    tables: Mapping[str, frozenset[str]] = field(default_factory=dict)
    capabilities: frozenset[str] = frozenset()
    organization_admin: bool = False
    applications: frozenset[str] = frozenset()
    token_scopes: frozenset[str] | None = None
    everything: bool = False

    def table_actions(self, key: str) -> frozenset[str]:
        if self.everything:
            return frozenset(TABLE_ACTIONS)
        return self.tables.get(key, frozenset())

    def allows_capability(self, capability: str | None) -> bool:
        if capability is None or self.everything:
            return True
        if capability == ORGANIZATION_ADMIN:
            return self.organization_admin
        return capability in self.capabilities

    def allows_scope(self, scope: str | None) -> bool:
        return scope is None or self.everything or self.token_scopes is None or scope in self.token_scopes

    def allows_application(self, app_label: str | None) -> bool:
        return app_label is None or self.everything or app_label in self.applications

    def allows_method(self, view: type, method: str) -> bool:
        if self.everything or self.token_scopes is None:
            return True
        return method in tuple(getattr(view, "token_methods", ()) or ())


def full_audience() -> Audience:
    """Everything, unfiltered: only for the contract tests, never for an HTTP request."""
    return Audience(everything=True)


def _organization_audience(user: Any) -> dict[str, Any]:
    """What ``user`` reaches in the current tenant context (materialized values only)."""
    from django.core.exceptions import PermissionDenied

    from skeleton.access.capabilities import capabilities_for
    from skeleton.access.policies import has_table_permission, is_organization_admin
    from skeleton.common.exceptions import ApplicationUnavailable
    from skeleton.common.models import SecuredRecord
    from skeleton.system.app_registry import all_apps
    from skeleton.system.applications import require_application_enabled
    from skeleton.system.record_operations import require_operation
    from skeleton.system.registry import all_specs

    def enabled(app_label: str) -> bool:
        try:
            require_application_enabled(app_label)
        except (ApplicationUnavailable, PermissionDenied):
            return False
        return True

    tables = {}
    for spec in all_specs():
        if not spec.expose_api or not issubclass(spec.model, SecuredRecord):
            continue
        if not enabled(spec.model._meta.app_label):
            continue
        actions = set()
        for action in TABLE_ACTIONS:
            if action in {"create", "update", "delete", "restore"}:
                try:
                    require_operation(spec, action)
                except PermissionDenied:
                    continue  # A declared business service without this operation always denies.
            if has_table_permission(spec.model, user=user, action=action):
                actions.add(action)
        if actions:
            tables[spec.model._meta.label_lower] = frozenset(actions)
    return {
        "tables": tables,
        "capabilities": capabilities_for(user),
        "organization_admin": is_organization_admin(user),
        "applications": frozenset(manifest.app_label for manifest in all_apps() if enabled(manifest.app_label)),
    }


def audience_for(request: Any) -> Audience:
    """The actor's audience, one read-only context per organization (an aggregate route, #74).

    A token adds its scopes to each context, so a table action outside them is absent; a token
    bound to an organization reads only that one.
    """
    from skeleton.tenancy.aggregate import for_each_organization

    token = getattr(request, "api_token", None)
    scopes = frozenset(token.scopes) if token is not None else None
    bound = token.bound_organization_id if token is not None else None
    source = f"http:token:{token.prefix}" if token is not None else "http"

    def read(organization_id: Any) -> dict[str, Any] | None:
        if bound is not None and organization_id != bound:
            return None
        return _organization_audience(request.user)

    tables: dict[str, frozenset[str]] = {}
    capabilities: set[str] = set()
    organization_admin = False
    applications: set[str] = set()
    for result in for_each_organization(
        request.user, read, source=source, request_id=request.request_id, credential_scopes=scopes
    ):
        value = result.value
        if value is None:
            continue
        for key, actions in value["tables"].items():
            tables[key] = tables.get(key, frozenset()) | actions
        capabilities |= value["capabilities"]
        organization_admin = organization_admin or value["organization_admin"]
        applications |= value["applications"]
    return Audience(
        tables=tables,
        capabilities=frozenset(capabilities),
        organization_admin=organization_admin,
        applications=frozenset(applications),
        token_scopes=scopes,
    )


# --- Field types from _meta ----------------------------------------------------------------

_INTEGER_TYPES = {
    "AutoField",
    "BigAutoField",
    "SmallAutoField",
    "IntegerField",
    "BigIntegerField",
    "SmallIntegerField",
}
_POSITIVE_TYPES = {"PositiveIntegerField", "PositiveBigIntegerField", "PositiveSmallIntegerField"}
_STRING_FORMATS = {
    "UUIDField": "uuid",
    "DateTimeField": "date-time",
    "DateField": "date",
    "TimeField": "time",
    "EmailField": "email",
    "URLField": "uri",
}
_STRING_TYPES = {"CharField", "TextField", "SlugField", "FilePathField", "GenericIPAddressField", "DurationField"}


def internal_type_schema(kind: str, model_field: Any = None) -> Schema:
    """JSON Schema of a Django internal type, as the API renders it (``rest_framework`` JSON).

    Unknown types are documented as any value rather than guessed.
    """
    if kind in _INTEGER_TYPES:
        return dict(INTEGER)
    if kind in _POSITIVE_TYPES:
        return {"type": "integer", "minimum": 0}
    if kind in {"FloatField", "DecimalField"}:
        return {"type": "number"}
    if kind == "BooleanField":
        return dict(BOOLEAN)
    if kind in _STRING_FORMATS:
        return string(format=_STRING_FORMATS[kind])
    if kind in _STRING_TYPES:
        schema = string()
        max_length = getattr(model_field, "max_length", None)
        if kind in {"CharField", "SlugField"} and isinstance(max_length, int):
            schema["maxLength"] = max_length
        return schema
    if kind == "JSONField":
        return {}
    if kind == "ArrayField" and model_field is not None and hasattr(model_field, "base_field"):
        return array(field_schema(model_field.base_field))
    return {}


def field_schema(model_field: Any, *, hidden_references: bool = False) -> Schema:
    """Schema of one concrete field; a relation is its target's key (a UUID).

    ``hidden_references``: the gateway answers ``null`` for a reference the actor cannot read
    (``records_data``), so every relation it renders accepts ``null``.
    """
    from skeleton.common.migration_operations import field_internal_type

    kind = field_internal_type(model_field)
    target = model_field.target_field if model_field.is_relation else model_field
    schema = internal_type_schema(kind, target)
    choices = getattr(model_field, "flatchoices", None) if not model_field.is_relation else None
    if choices:
        schema["enum"] = [value for value, _ in choices]
    if model_field.null or (hidden_references and model_field.is_relation):
        schema = nullable(schema)
    return schema


def model_fields(model: Any, names: Mapping[str, str] | tuple[str, ...], **extra: Schema) -> Schema:
    """Closed object of ``model`` fields; ``names`` maps the rendered name to the field name."""
    mapping = names if isinstance(names, Mapping) else {name: name for name in names}
    properties = {key: field_schema(model._meta.get_field(name)) for key, name in mapping.items()}
    properties.update(extra)
    return obj(properties)


def model_label_fields(label: str, names: Mapping[str, str] | tuple[str, ...], **extra: Schema) -> Callable[[], Schema]:
    """Lazy :func:`model_fields` by model label, for ``openapi`` declarations at import time."""

    def build() -> Schema:
        from django.apps import apps

        return model_fields(apps.get_model(label), names, **extra)

    return build


def _required_on_create(model_field: Any) -> bool:
    return not model_field.null and not model_field.blank and not model_field.has_default()


# --- Routes -------------------------------------------------------------------------------

_PARAMETER = re.compile(r"<(?:(?P<converter>[^>:]+):)?(?P<name>\w+)>")
_CONVERTERS: dict[str, Schema] = {"uuid": UUID, "int": INTEGER}
API_PREFIX = "api/v1/"


@dataclass(frozen=True)
class Route:
    """One ``/api/v1/`` URL pattern: its OpenAPI path, parameters and view class."""

    path: str
    parameters: Mapping[str, str]
    view: type
    application: str | None


def api_routes(urlconf: str | None = None) -> tuple[Route, ...]:
    """Every ``/api/v1/`` URL pattern of the URL tree, in resolution order."""
    from django.urls import get_resolver
    from django.urls.resolvers import RoutePattern, URLResolver

    from skeleton.system.app_registry import all_apps

    api_apps = tuple(manifest.app_label for manifest in all_apps() if manifest.api_urls)
    found: list[Route] = []

    def walk(patterns: Any, prefix: str) -> None:
        for pattern in patterns:
            if not isinstance(pattern.pattern, RoutePattern):
                continue  # Regex routes are not part of the API.
            route = prefix + str(pattern.pattern)
            if isinstance(pattern, URLResolver):
                if API_PREFIX.startswith(route) or route.startswith(API_PREFIX) or not route:
                    walk(pattern.url_patterns, route)
                continue
            if not route.startswith(API_PREFIX):
                continue
            callback = pattern.callback
            view = getattr(callback, "view_class", None) or getattr(callback, "cls", None)
            if view is None:
                continue
            application = next(
                (
                    label
                    for label in api_apps
                    if route.startswith(f"{API_PREFIX}o/<uuid:organization_id>/apps/{label}/")
                ),
                None,
            )
            parameters = {match["name"]: match["converter"] or "str" for match in _PARAMETER.finditer(route)}
            found.append(Route("/" + _PARAMETER.sub(r"{\g<name>}", route), parameters, view, application))

    walk(get_resolver(urlconf).url_patterns, "")
    return tuple(found)


Expanded = tuple[str, str, Operation]  # (path, method, operation)


def _view_operations(route: Route, audience: Audience) -> Iterator[Expanded]:
    document = getattr(route.view, "openapi", None)
    if hasattr(document, "expand"):
        yield from document.expand(route, audience)
        return
    if isinstance(document, Mapping):
        detail = getattr(route.view, "openapi_detail", "sysid") in route.parameters
        for method, declared in document.items():
            for operation in declared if isinstance(declared, tuple) else (declared,):
                if operation.detail is None or operation.detail == detail:
                    yield route.path, method.upper(), operation
        return
    for method in getattr(route.view, "http_method_names", ()):
        if method not in {"head", "options"} and hasattr(route.view, method):
            yield route.path, method.upper(), Operation(summary=f"{route.view.__name__}.{method}", status="2XX")


# --- Generic gateway -------------------------------------------------------------------------


def _visible_specs(audience: Audience) -> list[tuple[Any, frozenset[str]]]:
    from skeleton.common.models import SecuredRecord
    from skeleton.system.registry import all_specs

    result = []
    for spec in all_specs():
        if not spec.expose_api or not issubclass(spec.model, SecuredRecord):
            continue
        actions = audience.table_actions(spec.model._meta.label_lower)
        if actions:
            result.append((spec, actions))
    return result


def _record_name(spec: Any) -> str:
    return f"Record.{spec.model._meta.label_lower}"


def _input_name(spec: Any) -> str:
    return f"RecordInput.{spec.model._meta.label_lower}"


def record_schema(spec: Any) -> Schema:
    """What the gateway renders for one record of ``spec``, without its sensitive fields."""
    from skeleton.web.services import SYSTEM_FIELDS

    sensitive = set(spec.sensitive_fields)
    names = [name for name in dict.fromkeys((*SYSTEM_FIELDS, *spec.read_fields)) if name not in sensitive]
    properties = {name: field_schema(spec.model._meta.get_field(name), hidden_references=True) for name in names}
    # A sensitive read field is rendered but never described, so the object stays open.
    closed = not sensitive & set(spec.read_fields)
    schema = obj(properties, closed=closed)
    schema["title"] = str(spec.label or spec.model._meta.verbose_name)
    return schema


def record_input_schema(spec: Any, *, creating: bool) -> Schema:
    sensitive = set(spec.sensitive_fields)
    names = [name for name in spec.write_fields if name not in sensitive]
    fields = {name: spec.model._meta.get_field(name) for name in names}
    required = tuple(name for name, item in fields.items() if creating and _required_on_create(item))
    return obj(
        {name: field_schema(item) for name, item in fields.items()},
        required=required,
        closed=not sensitive & set(spec.write_fields),
    )


def access_schema(model: Any) -> Schema:
    from skeleton.web.services import ACCESS_FIELDS, SYSTEM_FIELDS

    return model_fields(model, (*SYSTEM_FIELDS, *ACCESS_FIELDS))


def access_input_schema(model: Any) -> Schema:
    from skeleton.web.services import ACCESS_FIELDS

    return obj({name: field_schema(model._meta.get_field(name)) for name in ACCESS_FIELDS}, required=())


def _filters(spec: Any) -> tuple[tuple[str, Schema, str], ...]:
    """The equality filters ``filter_records`` accepts, minus the sensitive fields."""
    from django.db import models

    sensitive = set(spec.sensitive_fields)
    names = [
        name
        for name in spec.read_fields
        if name not in sensitive
        and not spec.model._meta.get_field(name).is_relation
        and not isinstance(spec.model._meta.get_field(name), models.JSONField)
    ]
    result = []
    for name in dict.fromkeys(("sysid", "owner", "visibility", *names)):
        schema = field_schema(spec.model._meta.get_field(name))
        result.append((name, schema, f"Igualdade em {name}."))
    return tuple(result)


REFERENCE_OPTION = obj({"sysid": UUID, "label": string()})


@dataclass(frozen=True)
class TableRoutes:
    """``openapi`` of the gateway views: one path per visible table instead of ``{table_key}``."""

    kind: str

    def expand(self, route: Route, audience: Audience) -> Iterator[Expanded]:
        for spec, actions in _visible_specs(audience):
            path = route.path.replace("{table_key}", spec.model._meta.label_lower)
            yield from getattr(self, "_" + self.kind.replace("-", "_"))(spec, actions, route, path)

    def _records(self, spec: Any, actions: frozenset[str], route: Route, path: str) -> Iterator[Expanded]:
        record, label = ref(_record_name(spec)), spec.label or spec.model._meta.label_lower
        if "sysid" not in route.parameters:
            if "read" in actions:
                yield path, "GET", Operation(f"Lista {label}", record, paginated=True, query=_filters(spec))
            if "create" in actions:
                yield (
                    path,
                    "POST",
                    Operation(
                        f"Cria {label}", record, status=201, etag=True, request=ref(_input_name(spec) + ".create")
                    ),
                )
            return
        if "read" in actions:
            yield path, "GET", Operation(f"Lê {label}", record, etag=True)
        if "update" in actions:
            yield (
                path,
                "PATCH",
                Operation(
                    f"Altera {label}", record, etag=True, if_match=True, request=ref(_input_name(spec) + ".update")
                ),
            )
        if "delete" in actions:
            yield path, "DELETE", Operation(f"Remove {label} (soft delete)", None, status=204, if_match=True)

    def _restore(self, spec: Any, actions: frozenset[str], route: Route, path: str) -> Iterator[Expanded]:
        if "restore" in actions:
            yield (
                path,
                "POST",
                Operation(
                    f"Restaura {spec.label or spec.model._meta.label_lower}",
                    ref(_record_name(spec)),
                    etag=True,
                    if_match=True,
                ),
            )

    def _access(self, spec: Any, actions: frozenset[str], route: Route, path: str) -> Iterator[Expanded]:
        if "manage_access" not in actions:
            return
        model = spec.model
        yield path, "GET", Operation("Lê dono, visibilidade e grupo", lambda: access_schema(model), etag=True)
        yield (
            path,
            "PATCH",
            Operation(
                "Altera dono, visibilidade ou grupo",
                lambda: access_schema(model),
                etag=True,
                if_match=True,
                request=lambda: access_input_schema(model),
            ),
        )

    def _references(self, spec: Any, actions: frozenset[str], route: Route, path: str) -> Iterator[Expanded]:
        from skeleton.web.references import ACCESS_REFERENCE_FIELDS

        sensitive = set(spec.sensitive_fields)
        names = []
        if actions & {"create", "update"}:
            names.extend(name for name in spec.write_fields if name not in sensitive)
        if "manage_access" in actions:
            names.extend(sorted(ACCESS_REFERENCE_FIELDS))
        for name in dict.fromkeys(names):
            model_field = spec.model._meta.get_field(name)
            if not model_field.is_relation or model_field.many_to_many or model_field.one_to_many:
                continue
            query = (
                ("record", UUID, "Registro de origem; obrigatório com access=1."),
                ("access", string(enum=["0", "1"]), "1 para escolher dono ou grupo de um registro."),
            )
            yield (
                path.replace("{field_name}", name),
                "GET",
                Operation(f"Opções da referência {name}", REFERENCE_OPTION, paginated=True, query=query),
            )

    def _attachments(self, spec: Any, actions: frozenset[str], route: Route, path: str) -> Iterator[Expanded]:
        if "read" not in actions:
            return
        yield path, "GET", Operation("Lista os anexos do registro", results(ref("Attachment")), scope="files:read")
        if spec.attachments is None:
            return  # Uploads need the table's AttachmentPolicy; listing and removal do not.
        yield (
            path,
            "POST",
            Operation(
                "Envia um anexo (campo file)",
                ref("Attachment"),
                status=201,
                etag=True,
                multipart=True,
                scope="files:write",
            ),
        )

    def _attachment(self, spec: Any, actions: frozenset[str], route: Route, path: str) -> Iterator[Expanded]:
        if "read" in actions:
            yield path, "DELETE", Operation("Remove o anexo", None, status=204, if_match=True, scope="files:write")

    def _attachment_restore(self, spec: Any, actions: frozenset[str], route: Route, path: str) -> Iterator[Expanded]:
        if "read" in actions:
            yield (
                path,
                "POST",
                Operation("Restaura o anexo", ref("Attachment"), etag=True, if_match=True, scope="files:write"),
            )


# --- Access administration (DedicatedView) ------------------------------------------------


def dedicated_schema(resource: str) -> Schema:
    """What ``dedicated_data`` renders for ``resource``."""
    from django.apps import apps

    from skeleton.api.serializers import DEDICATED, DEDICATED_EXTRA
    from skeleton.web.services import SYSTEM_FIELDS

    label, fields = DEDICATED[resource]
    model = apps.get_model(label)
    extra = {}
    if resource == "memberships":
        extra["account_type"] = field_schema(apps.get_model("sk_accounts", "User")._meta.get_field("account_type"))
    return model_fields(model, (*SYSTEM_FIELDS, *fields, *DEDICATED_EXTRA.get(resource, ())), **extra)


def dedicated_input_schema(resource: str, *, creating: bool) -> Schema:
    from django.apps import apps

    from skeleton.api.serializers import DEDICATED, dedicated_required

    label, fields = DEDICATED[resource]
    model = apps.get_model(label)
    if resource == "memberships" and not creating:
        fields = ("is_active",)
    required = dedicated_required(resource, model, fields) if creating else ()
    return obj({name: field_schema(model._meta.get_field(name)) for name in fields}, required=required)


@dataclass(frozen=True)
class DedicatedRoutes:
    """``openapi`` of ``DedicatedView``: one path per resource instead of ``{resource}``."""

    def expand(self, route: Route, audience: Audience) -> Iterator[Expanded]:
        from skeleton.access.capabilities import RESOURCE_CAPABILITIES, RESOURCE_VIEW_CAPABILITIES
        from skeleton.api.serializers import DEDICATED, LINKS

        detail = "sysid" in route.parameters
        for resource in DEDICATED:
            path = route.path.replace("{resource}", resource)
            view, change = RESOURCE_VIEW_CAPABILITIES[resource], RESOURCE_CAPABILITIES[resource]
            data = ref(f"Dedicated.{resource}")

            def schema(resource: str = resource, creating: bool = True) -> Callable[[], Schema]:
                return lambda: dedicated_input_schema(resource, creating=creating)

            if not detail:
                yield path, "GET", Operation(f"Lista {resource}", data, paginated=True, capability=view)
                yield (
                    path,
                    "POST",
                    Operation(f"Cria {resource}", data, status=201, etag=True, request=schema(), capability=change),
                )
                continue
            yield path, "GET", Operation(f"Lê {resource}", data, etag=True, capability=view)
            if resource not in LINKS and resource != "invitations":
                yield (
                    path,
                    "PATCH",
                    Operation(
                        f"Altera {resource}",
                        data,
                        etag=True,
                        if_match=True,
                        request=schema(creating=False),
                        capability=change,
                    ),
                )
            yield path, "DELETE", Operation(f"Remove {resource}", None, status=204, if_match=True, capability=change)


# --- Document --------------------------------------------------------------------------------


def _components(audience: Audience, specs: list[tuple[Any, frozenset[str]]]) -> dict[str, Any]:
    from django.conf import settings

    from skeleton.api.serializers import DEDICATED
    from skeleton.files.services import attachment_schema

    schemas: dict[str, Schema] = {
        "Error": obj(
            {
                "code": string(description="Código estável do erro."),
                "detail": string(description="Mensagem para pessoas."),
                "fields": {
                    "type": "object",
                    "description": "Erros por campo.",
                    "additionalProperties": {"anyOf": [array(string()), string()]},
                },
            },
            required=("code", "detail"),
        ),
        "Page": obj(
            {
                "count": {"type": "integer", "minimum": 0},
                "page": {"type": "integer", "minimum": 1},
                "page_size": {"type": "integer", "minimum": 1, "maximum": 100},
                "results": array({}),
            },
            closed=False,
        ),
        "Attachment": attachment_schema(),
    }
    for spec, _ in specs:
        schemas[_record_name(spec)] = record_schema(spec)
        schemas[_input_name(spec) + ".create"] = record_input_schema(spec, creating=True)
        schemas[_input_name(spec) + ".update"] = record_input_schema(spec, creating=False)
    for resource in DEDICATED:
        schemas[f"Dedicated.{resource}"] = dedicated_schema(resource)
    error = {"description": "Erro {code, detail, fields}.", "content": {"application/json": {"schema": ref("Error")}}}
    return {
        "schemas": schemas,
        "parameters": {
            "Page": {
                "name": "page",
                "in": "query",
                "required": False,
                "schema": {"type": "integer", "minimum": 1, "default": 1},
            },
            "PageSize": {
                "name": "page_size",
                "in": "query",
                "required": False,
                "schema": {"type": "integer", "minimum": 1, "maximum": 100, "default": 25},
            },
            "IfMatch": {
                "name": "If-Match",
                "in": "header",
                "required": True,
                "description": 'Revisão atual entre aspas, como o ETag da leitura: "3".',
                "schema": string(pattern='^"[0-9]+(-[0-9]+)?"$'),
            },
        },
        "headers": {
            "ETag": {
                "description": 'Revisão do recurso entre aspas ("3"); devolva-a em If-Match para alterar.',
                "schema": string(),
            }
        },
        "responses": {
            "Error": error,
            "PreconditionFailed": {**error, "description": "412 revision_conflict: a revisão mudou."},
            "PreconditionRequired": {**error, "description": "428 revision_required: envie If-Match."},
        },
        "securitySchemes": {
            "bearerAuth": {
                "type": "http",
                "scheme": "bearer",
                "description": "Token de API (#45); vale só nos métodos que a operação aceita por token.",
            },
            "sessionAuth": {
                "type": "apiKey",
                "in": "cookie",
                "name": settings.SESSION_COOKIE_NAME,
                "description": "Sessão do navegador; métodos de escrita exigem o token CSRF.",
            },
        },
    }


def _operation_id(method: str, path: str) -> str:
    return method.lower() + "_" + re.sub(r"[^A-Za-z0-9]+", "_", path.strip("/")).strip("_")


def _openapi_operation(route: Route, path: str, method: str, operation: Operation, undocumented: bool) -> Schema:
    parameters: list[Schema] = []
    for name in re.findall(r"{(\w+)}", path):
        schema = _CONVERTERS.get(route.parameters.get(name, "str"), string())
        parameters.append({"name": name, "in": "path", "required": True, "schema": dict(schema)})
    for name, schema, description in operation.query:
        parameters.append(
            {"name": name, "in": "query", "required": False, "schema": schema, "description": description}
        )
    if operation.paginated:
        parameters.extend([{"$ref": "#/components/parameters/Page"}, {"$ref": "#/components/parameters/PageSize"}])
    if operation.if_match:
        if operation.if_match_optional:
            parameters.append(
                {
                    "name": "If-Match",
                    "in": "header",
                    "required": False,
                    "description": "Obrigatório quando o recurso já existe.",
                    "schema": string(),
                }
            )
        else:
            parameters.append({"$ref": "#/components/parameters/IfMatch"})
    success: Schema = {"description": operation.summary}
    body = _resolve(operation.response)
    if body is not None and operation.status != 204:
        if operation.paginated:
            page = obj(
                {"results": array(body), **operation.page_extra}, required=tuple(operation.page_extra), closed=False
            )
            body = {"allOf": [ref("Page"), page]}
        success["content"] = {"application/json": {"schema": body}}
    if operation.etag:
        success["headers"] = {"ETag": {"$ref": "#/components/headers/ETag"}}
    responses: Schema = {str(operation.status): success}
    for status, description in operation.extra_responses.items():
        responses[status] = {**_error_response(), "description": description}
    if operation.if_match:
        responses["412"] = {"$ref": "#/components/responses/PreconditionFailed"}
        responses["428"] = {"$ref": "#/components/responses/PreconditionRequired"}
    responses["default"] = {"$ref": "#/components/responses/Error"}
    token = method in tuple(getattr(route.view, "token_methods", ()) or ())
    result: Schema = {
        "operationId": _operation_id(method, path),
        "summary": operation.summary,
        "tags": [_tag(route)],
        "parameters": parameters,
        "responses": responses,
        "security": [{"bearerAuth": []}, {"sessionAuth": []}] if token else [{"sessionAuth": []}],
    }
    if operation.description:
        result["description"] = operation.description
    request = _resolve(operation.request)
    if operation.multipart:
        result["requestBody"] = {
            "required": True,
            "content": {
                "multipart/form-data": {"schema": obj({"file": string(contentMediaType="application/octet-stream")})}
            },
        }
    elif request is not None:
        result["requestBody"] = {
            "required": operation.request_required,
            "content": {"application/json": {"schema": request}},
        }
    if undocumented:
        result[UNDOCUMENTED] = True
    return result


def _error_response() -> Schema:
    return {"content": {"application/json": {"schema": ref("Error")}}}


def _tag(route: Route) -> str:
    if route.application:
        return f"apps.{route.application}"
    module = route.view.__module__.split(".")
    return module[1] if len(module) > 1 and module[0] == "skeleton" else module[0]


def build_schema(audience: Audience, *, urlconf: str | None = None) -> Schema:
    """The OpenAPI 3.1 document of what ``audience`` reaches."""
    from skeleton.versioning import core_version

    paths: dict[str, dict[str, Schema]] = {}
    for route in api_routes(urlconf):
        if not audience.allows_application(route.application):
            continue
        undocumented = getattr(route.view, "openapi", None) is None
        for path, method, operation in _view_operations(route, audience):
            if not audience.allows_method(route.view, method):
                continue
            if not audience.allows_capability(operation.capability) or not audience.allows_scope(operation.scope):
                continue
            item = paths.setdefault(path, {})
            if method.lower() in item:
                continue  # The first route that resolves wins, as in Django.
            item[method.lower()] = _openapi_operation(route, path, method, operation, undocumented)
    return {
        "openapi": OPENAPI_VERSION,
        "info": {
            "title": "Skeleton API",
            "version": core_version()["version"] or "0",
            "description": (
                "Contrato de /api/v1/ gerado do código: só as tabelas, operações e apps que o ator "
                "alcança. Erros seguem {code, detail, fields}."
            ),
        },
        "servers": [{"url": "/"}],
        "paths": {path: paths[path] for path in sorted(paths)},
        "components": _components(audience, _visible_specs(audience)),
    }
