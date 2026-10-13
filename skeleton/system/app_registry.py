"""Code-owned extension contracts. Registration never queries the database or runs DDL."""
from dataclasses import dataclass
from pathlib import Path
import re

from django.core.exceptions import ValidationError


@dataclass(frozen=True)
class ConfigField:
    name: str
    value_type: type
    default: object
    max_length: int = 200


@dataclass(frozen=True)
class MenuEntry:
    label: str
    url_name: str
    table_key: str
    action: str = 'read'


@dataclass(frozen=True)
class AppManifest:
    app_label: str
    label: str
    description: str
    dependencies: tuple[str, ...] = ()
    configuration_fields: tuple[ConfigField, ...] = ()
    navigation: tuple[MenuEntry, ...] = ()
    urls: str | None = None
    api_urls: str | None = None
    default_enabled: bool = True
    documentation_root: str | None = None
    app_id: str | None = None
    version: str = "0.1.0"
    package_name: str | None = None
    origin: str = "project-local"
    dependency_versions: tuple[tuple[str, str], ...] = ()
    canonical: dict | None = None

    @classmethod
    def from_json(cls, path, *, app_config):
        """Load one canonical contract, resolving runtime modules relative to AppConfig."""
        from skeleton.system.packages import load_manifest, PackageError
        contract = load_manifest(path)
        if contract["kind"] != "app":
            raise PackageError("AppManifest requires an application contract.")
        expected = contract["app_config"].rsplit(".", 1)[1]
        if app_config.label != contract["app_label"] or app_config.__class__.__name__ != expected:
            raise PackageError("AppConfig identity differs from its canonical manifest.")
        managed = f'applications.{contract["package_name"]}'
        if app_config.name.startswith("applications.") and app_config.name != managed:
            raise PackageError("Managed application package identity mismatch.")
        runtime = contract["runtime"]
        root = Path(app_config.path).resolve()
        documentation = (root / runtime["documentation_root"]).resolve()
        if not documentation.is_relative_to(root):
            raise PackageError("Application documentation escapes its root.")
        def module(name):
            return app_config.name + name[len(managed):] if name else None
        origin, default_enabled = "project-local", runtime["default_enabled"]
        from django.conf import settings
        inventory = getattr(settings, "SKELETON_DEPLOYMENT_STATE", None)
        for entry in getattr(inventory, "apps", ()):
            data = entry.get("manifest", {})
            if data.get("app_id") == contract["app_id"] and entry.get("state") in {"installed", "project-local"}:
                if data != contract or Path(entry["path"]).resolve() != root:
                    raise PackageError("Application contract differs from active deployment inventory.")
                origin = entry["state"]
                if origin == "installed" and entry.get("activation_policy") == "explicit":
                    default_enabled = False
        return cls(
            app_label=contract["app_label"], label=contract["name"],
            description=contract["description"],
            configuration_fields=tuple(
                ConfigField(field["name"], {"str": str, "int": int, "bool": bool}[field["type"]],
                            field["default"], field.get("max_length", 200))
                for field in runtime["configuration_fields"]),
            navigation=tuple(MenuEntry(**entry) for entry in runtime["navigation"]),
            urls=module(runtime.get("urls")), api_urls=module(runtime.get("api_urls")),
            default_enabled=default_enabled, documentation_root=str(documentation),
            app_id=contract["app_id"], version=contract["version"],
            package_name=contract["package_name"], origin=origin,
            dependency_versions=tuple(contract["dependencies"].items()), canonical=contract,
        )


_manifests = {}
CORE_APP_LABELS = frozenset({'sk_accounts', 'sk_tenancy', 'sk_access', 'sk_system', 'sk_audit'})


def register_app(manifest):
    if not isinstance(manifest, AppManifest) or not re.fullmatch(r'[a-z][a-z0-9_]*', manifest.app_label):
        raise ValidationError('Application requires a stable app label.')
    if len(set(manifest.dependencies)) != len(manifest.dependencies) or manifest.app_label in manifest.dependencies:
        raise ValidationError('Application dependencies must be unique and nonrecursive.')
    names = [field.name for field in manifest.configuration_fields]
    if len(names) != len(set(names)):
        raise ValidationError('Duplicate application configuration field.')
    for field in manifest.configuration_fields:
        if not re.fullmatch(r'[a-z][a-z0-9_]*', field.name) or field.value_type not in (str, bool, int):
            raise ValidationError('Application configuration supports named scalar fields only.')
        if any(part in field.name.lower() for part in ('secret', 'password', 'token', 'private_key')):
            raise ValidationError('Application secrets belong in deployment configuration.')
    from skeleton.versioning import normalize_version
    try:
        normalize_version(manifest.version)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    identity = manifest.app_id or manifest.app_label
    for existing in _manifests.values():
        if existing.app_label != manifest.app_label and (
                (existing.app_id or existing.app_label) == identity
                or (manifest.package_name and existing.package_name == manifest.package_name)):
            raise ValidationError("Duplicate application identity or package namespace.")
    if manifest.app_label in _manifests and _manifests[manifest.app_label] != manifest:
        raise ValidationError('Duplicate application manifest.')
    _manifests[manifest.app_label] = manifest
    return manifest


def unregister_app(app_label):
    """Remove registry metadata only; never drops tables or tenant state."""
    _manifests.pop(app_label, None)


def get_app(app_label):
    manifest = _manifests[app_label]
    if manifest.dependency_versions:
        from dataclasses import replace
        by_id = {item.app_id or item.app_label: item.app_label for item in _manifests.values()}
        return replace(manifest, dependencies=tuple(by_id.get(identity, identity) for identity, _ in manifest.dependency_versions))
    return manifest


def all_apps():
    return tuple(get_app(key) for key in sorted(_manifests))


def validate_configuration(manifest, values):
    if not isinstance(values, dict):
        raise ValidationError({'configuration': 'Use an object of declared fields.'})
    fields = {field.name: field for field in manifest.configuration_fields}
    if set(values) - fields.keys():
        raise ValidationError({'configuration': 'Unknown application configuration field.'})
    result = {}
    for name, field in fields.items():
        value = values.get(name, field.default)
        if type(value) is not field.value_type or (isinstance(value, str) and len(value) > field.max_length):
            raise ValidationError({'configuration': f'Invalid value for {name}.'})
        result[name] = value
    return result


def validate_dependencies():
    from skeleton.system.packages import validate_dependency_graph, PackageError
    canonical = [item.canonical for item in _manifests.values() if item.canonical is not None]
    if canonical:
        try:
            validate_dependency_graph(canonical)
        except PackageError as exc:
            raise ValidationError(str(exc)) from exc
    def visit(label, ancestors):
        if label in ancestors:
            raise ValidationError('Cyclic application dependency.')
        if label not in _manifests:
            raise ValidationError('Application dependency is not installed.')
        for dependency in get_app(label).dependencies:
            visit(dependency, ancestors | {label})
    for manifest in all_apps():
        visit(manifest.app_label, set())


def validate_documentation(manifest):
    if not manifest.documentation_root:
        raise ValidationError('Application requires a documentation root.')
    root = Path(manifest.documentation_root)
    requirements = {'spec.md': ('## Contract', '## Acceptance'),
                    'adr.md': ('## Context', '## Decision', '## Consequences')}
    for filename, headings in requirements.items():
        path = root / filename
        if not path.is_file():
            raise ValidationError(f'Application requires {filename}.')
        text = path.read_text(encoding='utf-8')
        if any(heading not in text for heading in headings):
            raise ValidationError(f'Application {filename} is missing required sections.')


def application_urlpatterns():
    from django.urls import include, path
    result = []
    for manifest in all_apps():
        if manifest.urls:
            result.append(path(f'o/<uuid:organization_id>/apps/{manifest.app_label}/', include(manifest.urls)))
        if manifest.api_urls:
            result.append(path(f'api/v1/o/<uuid:organization_id>/apps/{manifest.app_label}/', include(manifest.api_urls)))
    return result
