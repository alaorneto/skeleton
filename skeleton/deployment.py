"""Private, operator-generated deployment metadata; no ORM or code hot loading."""
from dataclasses import dataclass
import json
from pathlib import Path
import re
import sys

from django.apps import AppConfig
from django.core.exceptions import ImproperlyConfigured

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class DeploymentInventory:
    generation: int
    core: dict
    apps: tuple
    retained_schema_paths: tuple
    migration_archives: tuple
    frozen_infrastructure: dict | None = None

    @property
    def installed_apps(self):
        return tuple(item["manifest"]["app_config"] for item in self.apps if item["state"] == "installed")


def _private_path(value, base):
    if not isinstance(value, str) or not value:
        raise ImproperlyConfigured("Deployment inventory contains an invalid path.")
    path = Path(value)
    return str(path.resolve() if path.is_absolute() else (base / path).resolve())


def load_deployment_inventory(path):
    if not path:
        return DeploymentInventory(0, {}, (), (), ())
    source = Path(path).resolve()
    try:
        if source.stat().st_size > 4 * 1024 * 1024:
            raise ValueError("oversized")
        data = json.loads(source.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or type(data.get("generation")) is not int or data["generation"] < 0:
            raise ValueError("generation")
        core = data["core"]
        if not isinstance(core, dict):
            raise ValueError("core")
        core_files = core.get("files", {})
        if not isinstance(core_files, dict):
            raise ValueError("core file inventory")
        for name, info in core_files.items():
            if not name.startswith("skeleton/") or "\\" in name or ".." in Path(name).parts:
                raise ValueError("core file path")
            if not isinstance(info, dict) or not re.fullmatch(r"[a-f0-9]{64}", info.get("sha256", "")):
                raise ValueError("core file digest")
            if type(info.get("size")) is not int or info["size"] < 0:
                raise ValueError("core file size")
        if core.get("version") is not None and (not isinstance(core.get("digest"), str)
                                                or not re.fullmatch(r"[a-f0-9]{64}", core["digest"])):
            raise ValueError("core digest")
        apps = data["apps"]
        if not isinstance(apps, list):
            raise ValueError("apps")
        labels, packages, ids = set(), set(), set()
        normalized = []
        from skeleton.system.packages import validate_manifest
        for item in apps:
            if not isinstance(item, dict) or item.get("state") not in {"installed", "project-local", "schema_retained"}:
                raise ValueError("state")
            manifest = validate_manifest(item["manifest"])
            if manifest["kind"] != "app":
                raise ValueError("kind")
            label, package, app_id = manifest["app_label"], manifest["package_name"], manifest["app_id"]
            if label in labels or package in packages or app_id in ids:
                raise ValueError("duplicate identity")
            labels.add(label); packages.add(package); ids.add(app_id)
            digest = item.get("digest")
            if item["state"] != "project-local" or digest is not None:
                if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
                    raise ValueError("digest")
            normalized.append({**item, "manifest": manifest, "path": _private_path(item["path"], source.parent)})
        retained = data.get("retained_schema_paths", [])
        archives = data.get("migration_archives", [])
        if not isinstance(retained, list) or not isinstance(archives, list):
            raise ValueError("retained metadata")
        archive_labels = set()
        clean_archives = []
        for archive in archives:
            label, package = archive["app_label"], archive["package_name"]
            module = archive["migrations_module"]
            if not _IDENTIFIER.fullmatch(label) or not _IDENTIFIER.fullmatch(package):
                raise ValueError("archive identity")
            if not isinstance(module, str) or label in archive_labels or not module.endswith(".migrations") or not all(_IDENTIFIER.fullmatch(p) for p in module.split(".")):
                raise ValueError("archive module")
            if not any(a["state"] == "schema_retained" and a["manifest"]["app_label"] == label for a in normalized):
                raise ValueError("archive without retained app")
            archive_labels.add(label)
            clean_archives.append({**archive, "path": _private_path(archive["path"], source.parent)})
        if archive_labels != {a["manifest"]["app_label"] for a in normalized if a["state"] == "schema_retained"}:
            raise ValueError("missing archive")
        clean_retained = []
        for entry in retained:
            if isinstance(entry, dict):
                if set(entry) != {"path", "sha256"} or not re.fullmatch(r"[a-f0-9]{64}", entry["sha256"]):
                    raise ValueError("retained digest")
                clean_retained.append({**entry, "path": _private_path(entry["path"], source.parent)})
            else:
                clean_retained.append(_private_path(entry, source.parent))
        frozen = data.get("frozen_infrastructure")
        if frozen is not None:
            if not isinstance(frozen, dict) or set(frozen) != {"path", "sha256"} or not re.fullmatch(r"[a-f0-9]{64}", frozen["sha256"]):
                raise ValueError("frozen infrastructure reference")
            frozen = {**frozen, "path": _private_path(frozen["path"], source.parent)}
        return DeploymentInventory(data["generation"], core, tuple(normalized),
                                   tuple(clean_retained), tuple(clean_archives), frozen)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise ImproperlyConfigured("Invalid private deployment inventory.") from exc


class MigrationArchiveConfig(AppConfig):
    """Original label and genuine migrations, without business model/ready hooks."""
    def import_models(self):
        self.models = self.apps.all_models[self.label]
        self.models_module = None

    def ready(self):
        pass


def migration_archive_configs(inventory):
    configs, modules = [], {}
    for archive in inventory.migration_archives:
        path = Path(archive["path"])
        name = archive["migrations_module"].removesuffix(".migrations")
        if name != path.name or not name.startswith("archive_") or not path.is_dir():
            raise ImproperlyConfigured("Migration archive is not an isolated package.")
        import ast
        try:
            for entrypoint in (path / "__init__.py", path / "migrations" / "__init__.py"):
                body = ast.parse(entrypoint.read_text(encoding="utf-8")).body
                if body and not (len(body) == 1 and isinstance(body[0], ast.Expr)
                                 and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str)):
                    raise ValueError("archive entrypoint")
            if (path / "models.py").exists() or (path / "apps.py").exists():
                raise ValueError("business entrypoint")
        except (OSError, SyntaxError, ValueError) as exc:
            raise ImproperlyConfigured("Unsafe migration archive entrypoint.") from exc
        # The agent verifies archive contents and migration imports before activation.
        # Only the migrator adds this root; runtime never imports historical code.
        if str(path.parent) not in sys.path:
            sys.path.insert(0, str(path.parent))
        import importlib
        module = importlib.import_module(name)
        if Path(module.__file__).resolve() != (path / "__init__.py").resolve():
            raise ImproperlyConfigured("Migration archive import resolved outside its approved path.")
        config = MigrationArchiveConfig(name, module)
        config.label = archive["app_label"]
        config.path = str(path)
        configs.append(config)
        modules[config.label] = archive["migrations_module"]
    return configs, modules


def confirm_core(inventory):
    """Prove the loaded release's bytes and origin, not an inventory claim."""
    from skeleton.versioning import core_version
    actual = core_version()
    expected = inventory.core.get("version")
    if not expected:
        return actual, True
    files = inventory.core.get("files", {})
    expected_origin = inventory.core.get("origin", "installed")
    if (expected_origin not in {"installed", "source"} or expected != actual["version"]
            or actual["origin"] != expected_origin or not files):
        return actual, False
    import hashlib
    root = Path(__file__).resolve().parent.parent
    try:
        for name, info in files.items():
            path = root / name
            if not path.is_file() or path.is_symlink() or path.stat().st_size != info["size"]:
                return actual, False
            if hashlib.sha256(path.read_bytes()).hexdigest() != info["sha256"]:
                return actual, False
    except OSError:
        return actual, False
    return actual, True


def release_response(request, confirmation=None):
    from django.conf import settings
    from django.http import HttpResponseNotAllowed, JsonResponse
    request.get_host()
    if request.method not in {"GET", "HEAD"}:
        return HttpResponseNotAllowed(["GET", "HEAD"])
    inventory = settings.SKELETON_DEPLOYMENT_STATE
    actual, confirmed = confirmation if confirmation is not None else confirm_core(inventory)
    response = JsonResponse({"status": "ok" if confirmed else "release_mismatch", "generation": inventory.generation,
                             "core": {"version": actual["version"], "origin": actual["origin"],
                                      "digest": inventory.core.get("digest")}}, status=200 if confirmed else 503)
    response["Cache-Control"] = "no-store"
    if request.method == "HEAD":
        response.content = b""
    return response


class MaintenanceMiddleware:
    """Maintenance and confirmation execute before sessions or DB middleware.

    Application/admin requests are closed, including status views backed by the DB.
    The separate authenticated deployment agent remains the operation control plane.
    """
    def __init__(self, get_response):
        from django.conf import settings
        self.get_response = get_response
        self.marker = settings.SKELETON_MAINTENANCE_MARKER
        self.smoke_paths = frozenset(getattr(settings, "SKELETON_DEPLOYMENT_SMOKE_PATHS", ()))
        token_file = getattr(settings, "SKELETON_DEPLOYMENT_SMOKE_TOKEN_FILE", None)
        self.smoke_token = Path(token_file).read_text().strip() if token_file else None
        if self.smoke_token and (not self.smoke_token.isascii() or len(self.smoke_token) < 32):
            raise ImproperlyConfigured("Deployment smoke token must have at least 32 ASCII characters.")
        self.confirmation = confirm_core(settings.SKELETON_DEPLOYMENT_STATE)

    def _private_smoke(self, request):
        import secrets
        header = request.headers.get("X-Skeleton-Deployment-Smoke", "")
        return bool(self.smoke_token and request.method in {"GET", "HEAD"}
                    and request.path_info in self.smoke_paths
                    and header.isascii() and secrets.compare_digest(header, self.smoke_token))

    def __call__(self, request):
        if request.path_info == "/health/release/":
            return release_response(request, self.confirmation)
        if self.marker and Path(self.marker).exists() and not self._private_smoke(request):
            request.get_host()
            from django.http import JsonResponse
            response = JsonResponse({"status": "maintenance"}, status=503)
            response["Cache-Control"] = "no-store"
            response["Retry-After"] = "60"
            return response
        return self.get_response(request)
