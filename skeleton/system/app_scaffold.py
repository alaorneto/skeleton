"""Generate a new project application from the templates in ``app_template/``.

Rendering is pure and validated before anything touches the disk: the manifest
goes through :func:`skeleton.system.packages.validate_manifest` and the target
directory is created exclusively, so an existing app is never merged or
overwritten. Settings are never edited; the caller prints the next steps.
"""

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from string import Template

from django.utils.translation import gettext

from skeleton.system.packages import (
    PackageError,
    _identifier,
    _stable_id,
    manifest_template,
    validate_manifest,
)

TEMPLATE_ROOT = Path(__file__).resolve().parent / "app_template"
MODEL_NAME = re.compile(r"[A-Z][A-Za-z0-9]{0,62}\Z")
# Escrito em literais Python e Markdown: sem aspas, barras invertidas ou controles.
DISPLAY_NAME = re.compile(r"[^\"'\\\x00-\x1f\x7f]{1,200}\Z")


@dataclass(frozen=True)
class ApplicationScaffold:
    app_label: str
    package_name: str
    app_id: str
    publisher: str
    model: str
    verbose_name: str

    @property
    def config_class(self) -> str:
        return "".join(part.capitalize() for part in self.package_name.split("_")) + "Config"

    @property
    def service_class(self) -> str:
        return f"{self.model}Service"

    @property
    def model_lower(self) -> str:
        return self.model.lower()

    @property
    def table_key(self) -> str:
        return f"{self.app_label}.{self.model_lower}"

    @property
    def app_config(self) -> str:
        return f"applications.{self.package_name}.apps.{self.config_class}"


def build_scaffold(*, app_label, app_id, publisher, model="Item", package_name=None, verbose_name=None):
    """Validate the inputs with the package validators and return the scaffold."""
    package_name = package_name or app_label
    _identifier(app_label)
    _identifier(package_name)
    _stable_id(app_id)
    _stable_id(publisher)
    if not isinstance(model, str) or not MODEL_NAME.fullmatch(model):
        raise PackageError("Model name must be a CamelCase Python identifier.")
    verbose_name = verbose_name or app_label
    if not DISPLAY_NAME.fullmatch(verbose_name) or verbose_name != verbose_name.strip():
        raise PackageError("Display name must be 1-200 characters without quotes, backslashes or control characters.")
    return ApplicationScaffold(app_label, package_name, app_id, publisher, model, verbose_name)


def scaffold_manifest(scaffold):
    """Canonical v2 manifest from ``manifest_template`` with the generated runtime and service API."""
    manifest = manifest_template(
        app_id=scaffold.app_id,
        app_label=scaffold.app_label,
        package_name=scaffold.package_name,
        publisher=scaffold.publisher,
        app_config_class=scaffold.config_class,
        service_classes=[scaffold.service_class],
    )
    prefix = f"applications.{scaffold.package_name}"
    manifest["name"] = scaffold.verbose_name
    manifest["description"] = gettext("%(verbose_name)s: app gerada por skeleton_startapp") % {
        "verbose_name": scaffold.verbose_name
    }
    manifest["runtime"]["urls"] = f"{prefix}.urls"
    manifest["runtime"]["api_urls"] = f"{prefix}.api_urls"
    manifest["runtime"]["navigation"] = [
        {
            "label": scaffold.verbose_name,
            "url_name": f"{scaffold.app_label}:home",
            "table_key": scaffold.table_key,
            "action": "read",
        }
    ]
    return validate_manifest(manifest)


def render_files(scaffold):
    """Map each relative path of the new app to its rendered text."""
    values = {
        "app_label": scaffold.app_label,
        "app_id": scaffold.app_id,
        "config_class": scaffold.config_class,
        "service_class": scaffold.service_class,
        "model": scaffold.model,
        "model_lower": scaffold.model_lower,
        "table_key": scaffold.table_key,
        "verbose_name": scaffold.verbose_name,
    }
    layout = {
        "__init__.py": "__init__.py-tpl",
        "apps.py": "apps.py-tpl",
        "models.py": "models.py-tpl",
        "service_api.py": "service_api.py-tpl",
        "views.py": "views.py-tpl",
        "urls.py": "urls.py-tpl",
        "api_urls.py": "api_urls.py-tpl",
        f"templates/{scaffold.app_label}/home.html": "home.html-tpl",
        "docs/spec.md": "spec.md-tpl",
        "docs/adr.md": "adr.md-tpl",
        "docs/service-api.md": "service-api.md-tpl",
        "migrations/__init__.py": "__init__.py-tpl",
        "tests/__init__.py": "__init__.py-tpl",
        "tests/test_service_api.py": "test_service_api.py-tpl",
    }
    files = {
        target: Template((TEMPLATE_ROOT / source).read_text(encoding="utf-8")).substitute(values)
        for target, source in layout.items()
    }
    files["manifest.json"] = json.dumps(scaffold_manifest(scaffold), ensure_ascii=False, indent=2) + "\n"
    return files


def create_application(scaffold, directory):
    """Write the app under ``directory/<package_name>``; refuse an existing target."""
    files = render_files(scaffold)
    target = Path(directory) / scaffold.package_name
    if target.exists() or target.is_symlink():
        raise PackageError(f"Target directory already exists: {target}")
    target.mkdir(parents=True)
    try:
        for relative, text in files.items():
            path = target / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("xb") as handle:
                handle.write(text.encode("utf-8"))
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)
        raise
    return target, sorted(files)


def next_steps(scaffold):
    label = scaffold.app_label
    return [
        gettext('Adicione "%(app_config)s" a PROJECT_APPS em config/settings/project.py.')
        % {"app_config": scaffold.app_config},
        f"python manage.py makemigrations {label}",
        f"python manage.py record_infrastructure {label} --write",
        "python manage.py migrate",
        "python manage.py sync_table_catalog",
        "python manage.py check_skeleton_schema",
        gettext(
            "Atualize migrations.leaves no manifest.json (ex.: 0001_initial) e revise docs/spec.md, docs/adr.md "
            "e docs/service-api.md."
        ),
        gettext(
            "Exponha em service_api.py (%(service_class)s) só o que outras apps precisam; rode assert_service_contract em tests/."
        )
        % {"service_class": scaffold.service_class},
        f"python manage.py package doctor applications/{scaffold.package_name}",
        gettext(
            "Publique com: python manage.py package build --app-root applications/%(package_name)s --manifest applications/%(package_name)s/manifest.json --output <fora da app>.skapp --private-key <chave privada>"
        )
        % {"package_name": scaffold.package_name},
    ]
