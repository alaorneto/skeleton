"""The manifest of a new app (package init and skeleton_startapp)."""

from skeleton.system.presentation import FIELD_DIMENSIONS_CORE_MINIMUM

from .limits import SERVICE_API_CORE_MINIMUM
from .manifest import validate_manifest


def default_service_class(package_name):
    """Conventional service class of a package: tasks_board -> TasksBoardService."""
    return "".join(part.capitalize() for part in package_name.split("_")) + "Service"


def manifest_template(
    *,
    app_id,
    app_label,
    package_name,
    publisher,
    version="0.1.0",
    app_config_class="AppConfig",
    service_classes=None,
):
    """Canonical v2 source manifest; service_classes declares applications.<pkg>.service_api."""
    runtime = {
        "urls": None,
        "api_urls": None,
        "default_enabled": False,
        "configuration_fields": [],
        "navigation": [],
        "documentation_root": "docs",
        "administration": {"configuration_sections": [], "sections": [], "screens": []},
        "model_ui": [],
    }
    floor = FIELD_DIMENSIONS_CORE_MINIMUM
    if service_classes is not None:
        runtime["service_api"] = {
            "module": f"applications.{package_name}.service_api",
            "classes": list(service_classes),
        }
        floor = SERVICE_API_CORE_MINIMUM
    return validate_manifest(
        {
            "manifest_version": 2,
            "kind": "app",
            "app_id": app_id,
            "app_label": app_label,
            "package_name": package_name,
            "app_config": f"applications.{package_name}.apps.{app_config_class}",
            "name": app_label,
            "description": "Application release contract",
            "version": version,
            "publisher": publisher,
            "compatibility": {
                "skeleton": f">={floor},<1.0.0",
                "python": ">=3.12,<3.15",
                "django": ">=6.1,<6.2",
            },
            "dependencies": {},
            "conflicts": {},
            "runtime": runtime,
            "migrations": {"leaves": [], "from_versions": ">=0.0.0", "labels": [app_label]},
            "files": [],
            "python_dependencies": [],
            "lifecycle": {"data_retention": "retain", "maintenance": True, "backup": True},
        }
    )
