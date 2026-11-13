"""package doctor: every packaging, documentation, migration and dependency problem at once."""

from pathlib import Path

from .archive import _module_path
from .build import _scan_application_root, static_migration_leaves
from .catalog import validate_catalog
from .errors import PackageError
from .limits import SERVICE_API_DOCUMENT, SERVICE_API_HEADINGS, SUGGESTIBLE_PROBLEM_CODES
from .manifest import load_manifest, required_documents


def _doctor_dependencies(manifest, inventory, entries, report, provided=()):
    from skeleton.system.dependencies import catalog_candidates, resolve_dependencies

    core = inventory.get("core") or {}
    runtime = inventory.get("runtime") or {}
    installed = [
        entry["manifest"]
        for entry in inventory.get("apps", [])
        if entry.get("state") != "schema_retained" and entry["manifest"].get("app_id") != manifest["app_id"]
    ]
    candidates = [*([core["manifest"]] if core.get("manifest") else []), *installed, manifest]
    try:
        resolution = resolve_dependencies(
            candidates,
            core=core.get("version"),
            python=runtime.get("python"),
            django=runtime.get("django"),
            provided=provided,
        )
    except PackageError as exc:
        report("inventory_invalid", str(exc))
        return
    target = {
        key: value
        for key, value in (
            ("skeleton", core.get("version")),
            ("python", runtime.get("python")),
            ("django", runtime.get("django")),
        )
        if value
    }
    for problem in resolution.problems:
        detail = problem.as_dict()
        code = detail.pop("code")
        if code in SUGGESTIBLE_PROBLEM_CODES and entries:
            detail["suggestions"] = [
                {"component_id": entry["component_id"], "version": entry["version"], "digest": entry["digest"]}
                for entry in catalog_candidates(entries, problem.dependency, problem.required, target)
            ]
        report(code, problem.summary, **detail)


def _doctor_import_boundary(root, manifest, inventory, report):
    """Same static boundary as check E019/W019, with providers taken from the target inventory."""
    from skeleton.system.import_boundary import providers_from_manifests, scan_application

    installed = {
        entry["manifest"]["app_id"]: entry["manifest"]
        for entry in (inventory or {}).get("apps", [])
        if isinstance(entry.get("manifest"), dict)
        and entry["manifest"].get("app_id")
        and entry["manifest"]["app_id"] != manifest["app_id"]
    }
    for violation in scan_application(
        root,
        package="applications." + manifest["package_name"],
        providers=providers_from_manifests(installed),
        dependencies=manifest["dependencies"],
    ):
        values = {"path": violation.path, "line": violation.line, "module": violation.module}
        if violation.provider:
            values["provider"] = violation.provider
        if not violation.is_error:
            # Non-public core surfaces are reported without failing doctor until they become errors.
            values["severity"] = "warning"
        report(violation.code, violation.message, **values)


def doctor(app_root, manifest_path=None, *, inventory=None, catalog=None, trust_store=None, bundled_apps=()):
    """Report every packaging, documentation, migration and dependency problem of an app source.

    inventory is a target deployment inventory dict; catalog a signed catalogue envelope
    verified with trust_store; bundled_apps the SKELETON_BUNDLED_APPS names of the target (#254).
    Nothing is imported, built or written.
    """
    from skeleton.system.dependencies import catalog_candidates

    root = Path(app_root).resolve()
    manifest_file = Path(manifest_path).resolve() if manifest_path else root / "manifest.json"
    problems = []

    def report(code, message, **values):
        problems.append({"code": code, "message": message, **values})

    try:
        manifest = load_manifest(manifest_file)
    except (PackageError, OSError) as exc:
        report("manifest_invalid", str(exc))
        return {"ok": False, "problems": problems}
    if manifest["kind"] != "app":
        report("manifest_invalid", "doctor requires an application manifest.")
        return {"ok": False, "problems": problems}
    for code, message, path in _scan_application_root(root, manifest_file)[1]:
        report(code, message, path=path)
    if not (root / "apps.py").is_file():
        report("entrypoint_missing", "Application root requires apps.py.")
    service_api = manifest["runtime"].get("service_api")
    if service_api is None:
        report(
            "service_api_missing",
            "New releases declare runtime.service_api with the module and classes of the app's service API.",
        )
    elif not any((root / path).is_file() for path in _module_path(service_api["module"], manifest["package_name"])):
        report("service_api_module_missing", f"Declared service API module {service_api['module']} is absent.")
    if manifest["runtime"]["default_enabled"] is True:
        report(
            "default_enabled_ignored",
            "runtime.default_enabled is obsolete and ignored: installed apps start unavailable and disabled. "
            "Set it to false.",
            severity="warning",
        )
    documentation = root / manifest["runtime"]["documentation_root"]
    documents = {**required_documents(manifest), SERVICE_API_DOCUMENT: SERVICE_API_HEADINGS}
    for filename, headings in documents.items():
        path = documentation / filename
        if not path.is_file():
            report("documentation_missing", f"Required app documentation {filename} is missing.", path=filename)
            continue
        missing = [heading for heading in headings if heading not in path.read_text(encoding="utf-8")]
        if missing:
            report("documentation_sections", f"{filename} lacks sections {missing}.", path=filename)
    _doctor_import_boundary(root, manifest, inventory, report)
    actual = static_migration_leaves(root / "migrations", manifest["app_label"])
    declared = sorted(manifest["migrations"]["leaves"])
    if actual is None:
        report("migration_leaves_unverified", "Migration dependencies are not plain literals; check leaves manually.")
    elif actual != set(declared):
        report(
            "migration_leaves_mismatch",
            f"Manifest migrations.leaves {declared} differ from the migration files {sorted(actual)}.",
        )
    entries = []
    if catalog is not None:
        try:
            entries = validate_catalog(catalog, trust_store or {})["entries"]
        except PackageError as exc:
            report("catalog_invalid", str(exc))
    provided: tuple[str, ...] = ()
    if bundled_apps:
        from django.core.exceptions import ImproperlyConfigured

        from skeleton.conf.bundled import bundled_app_ids

        try:
            provided = bundled_app_ids(bundled_apps)
        except ImproperlyConfigured as exc:
            report("bundled_app_unknown", str(exc))
    if inventory is not None:
        _doctor_dependencies(manifest, inventory, entries, report, provided)
    elif entries:
        for dependency, required in sorted(manifest["dependencies"].items()):
            if dependency in provided:
                continue  # Shipped in the core wheel: never a catalogue release (#254).
            if not catalog_candidates(entries, dependency, required, {}):
                report(
                    "dependency_not_in_catalog",
                    f"No catalogue release of {dependency} satisfies {required}.",
                    dependency=dependency,
                    required=required,
                )
    failures = [problem for problem in problems if problem.get("severity") != "warning"]
    return {"ok": not failures, "app_id": manifest["app_id"], "problems": problems}


def validate_dependency_graph(manifests, *, core=None, python=None, django=None, target_environment=None, provided=()):
    """Validate core and all apps; return dependency-first application IDs.

    Wheel pins are compared only when their markers apply to target_environment,
    which accepts the string mapping from packaging.markers.default_environment().
    Raises the first problem; use skeleton.system.dependencies.resolve_dependencies
    for the complete structured report.
    """
    from skeleton.system.dependencies import resolve_dependencies

    resolution = resolve_dependencies(
        manifests, core=core, python=python, django=django, target_environment=target_environment, provided=provided
    )
    resolution.raise_for_problems()
    return list(resolution.order)
