"""Load apps under development from their own checkouts (#518): development and test only.

``SKELETON_DEV_APPS`` lists checkout directories of apps that live in their own git repository.
Each checkout is the app package itself (``manifest.json`` at its root), and the directory name
is the package name. The manifest is validated like any app manifest and must be ``kind: "app"``.

The app is imported under its managed namespace, ``applications.<package>``, exactly as in a
release, by a finder that maps that one module name to the checkout. Nothing is added to
``sys.path``, so a sibling directory never becomes importable and the core is never shadowed.
The production, migration, build and setup profiles refuse a non-empty value
(:func:`refuse_dev_apps`): in production an app only enters by a signed package.
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import importlib.util
import json
import sys
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

NAMESPACE = "applications"
REFUSED = (
    "SKELETON_DEV_APPS só é aceito nos perfis de desenvolvimento e teste; "
    "em produção uma app entra por pacote assinado."
)
_RESERVED_LABEL_PREFIXES = ("skeleton_", "sk_")
_MAX_MANIFEST_BYTES = 1024 * 1024


class DevAppFinder(importlib.abc.MetaPathFinder):
    """Maps ``applications.<package>`` to a checkout; provides the namespace when no project has one."""

    def __init__(self):
        self.packages: dict[str, Path] = {}

    def find_spec(self, fullname, path=None, target=None):
        if fullname == NAMESPACE:
            # Only reached when no project ``applications`` package exists (a standalone app repository).
            spec = importlib.machinery.ModuleSpec(NAMESPACE, None, is_package=True)
            spec.submodule_search_locations = []
            return spec
        prefix, _, package = fullname.partition(".")
        if prefix != NAMESPACE or package not in self.packages:
            return None
        root = self.packages[package]
        return importlib.util.spec_from_file_location(
            fullname, root / "__init__.py", submodule_search_locations=[str(root)]
        )


def _finder() -> DevAppFinder:
    for finder in sys.meta_path:
        if isinstance(finder, DevAppFinder):
            return finder
    finder = DevAppFinder()
    # Last: a real project package or module always wins, and a collision is refused below.
    sys.meta_path.append(finder)
    return finder


def _fail(path, reason):
    raise ImproperlyConfigured(f"SKELETON_DEV_APPS: {path}: {reason}")


def _manifest(root: Path) -> dict:
    from skeleton.system.packages import PackageError, validate_manifest

    source = root / "manifest.json"
    if not source.is_file():
        _fail(root, "manifest.json não encontrado na raiz do checkout.")
    if source.stat().st_size > _MAX_MANIFEST_BYTES:
        _fail(root, "manifest.json grande demais.")
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise ImproperlyConfigured(f"SKELETON_DEV_APPS: {root}: manifest.json não é JSON UTF-8 válido.") from exc
    if not isinstance(raw, dict) or raw.get("kind") != "app":
        _fail(root, 'o manifesto precisa ter kind "app".')
    label, config = raw.get("app_label"), raw.get("app_config")
    if not isinstance(label, str) or not isinstance(config, str):
        _fail(root, "o manifesto precisa declarar app_label e app_config.")
    if label.startswith(_RESERVED_LABEL_PREFIXES) or config.startswith("skeleton."):
        _fail(root, "label ou AppConfig reservado ao núcleo.")
    try:
        return validate_manifest(raw)
    except PackageError as exc:
        raise ImproperlyConfigured(f"SKELETON_DEV_APPS: {root}: manifesto inválido: {exc}") from exc


def _existing_origin(module: str) -> str | None:
    try:
        spec = importlib.util.find_spec(module)
    except (ImportError, ValueError):
        return None
    if spec is None:
        return None
    return spec.origin or ",".join(spec.submodule_search_locations or ()) or module


def dev_app_configs(paths) -> list[str]:
    """AppConfig paths of the checkouts in ``paths``, in order and without duplicates."""
    configs: list[str] = []
    for raw in paths:
        raw = str(raw).strip()
        if not raw:
            continue
        root = Path(raw).expanduser().resolve()
        if not root.is_dir():
            _fail(root, "o diretório não existe.")
        manifest = _manifest(root)
        package, config = manifest["package_name"], manifest["app_config"]
        if root.name != package:
            _fail(
                root,
                f"o diretório precisa se chamar {package!r}, o nome do pacote; "
                f"clone o repositório num diretório com esse nome.",
            )
        module = f"{NAMESPACE}.{package}"
        finder = _finder()
        current = finder.packages.get(package)
        if current is not None and current != root:
            _fail(root, f"o pacote {package!r} já foi carregado de {current}.")
        if current is None:
            origin = _existing_origin(module)
            if origin is not None:
                _fail(root, f"o pacote {module!r} já é importável de {origin}.")
            finder.packages[package] = root
        if config not in configs:
            configs.append(config)
    return configs


def refuse_dev_apps(base) -> None:
    """Called by every profile but development and test."""
    if tuple(base.get("SKELETON_DEV_APPS", ())):
        raise ImproperlyConfigured(REFUSED)
