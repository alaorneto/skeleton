"""System checks E012/E018/W018 (#98) and E019/W019 (#99) with registered fixture apps."""

import importlib
import sys
import textwrap
from dataclasses import replace
from types import SimpleNamespace

import pytest

from skeleton.system import checks
from skeleton.system.app_registry import AppManifest, ServiceAPIDeclaration, get_app, register_app, unregister_app

DOCS = {
    "spec.md": "## Contract\nFixture.\n## Acceptance\nFixture.\n",
    "adr.md": "## Context\nFixture.\n## Decision\nFixture.\n## Consequences\nFixture.\n",
    "service-api.md": "## Serviços\nFixture.\n## DTOs\nFixture.\n## Histórico de versões\nFixture.\n",
}
VALID_SERVICE = '''
from skeleton.service_api import ServiceAPI, service_method


class ProviderService(ServiceAPI):
    app_label = "{label}"

    @service_method(read_only=True, since="1.0")
    def total(self, *, actor) -> int:
        """Total.

        Autorização: read. Erros: nenhum. Efeitos: nenhum.
        """
        return 0
'''


def ids(errors, prefix):
    return sorted(error.id for error in errors if error.id.startswith(prefix))


def write(root, relative, text):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


@pytest.fixture
def fixture_app(tmp_path, monkeypatch):
    """Register app packages under tmp_path; returns a factory and unregisters them afterwards."""

    def forget():
        for name in [name for name in sys.modules if name == "svc_fixtures" or name.startswith("svc_fixtures.")]:
            del sys.modules[name]
        importlib.invalidate_caches()

    forget()
    monkeypatch.syspath_prepend(str(tmp_path))
    write(tmp_path, "svc_fixtures/__init__.py", "")
    labels, configs = [], {}
    real_apps = checks.apps

    class Apps:
        def get_app_config(self, label):
            if label in configs:
                return configs[label]
            return real_apps.get_app_config(label)

        def __getattr__(self, name):
            return getattr(real_apps, name)

    monkeypatch.setattr(checks, "apps", Apps())

    def make(label, *, files=None, docs=DOCS, service=True, dependencies=(), service_module=None):
        package = f"svc_fixtures.{label}"
        root = tmp_path / "svc_fixtures" / label
        write(root, "__init__.py", "")
        for name, text in (files or {}).items():
            write(root, name, text)
        for name, text in docs.items():
            write(root, f"docs/{name}", text)
        declaration = None
        if service:
            if service is True:
                write(root, "service_api.py", VALID_SERVICE.format(label=label))
            declaration = ServiceAPIDeclaration(service_module or f"{package}.service_api", ("ProviderService",))
        register_app(
            AppManifest(
                app_label=label,
                label=label,
                description="Fixture",
                dependencies=tuple(dependencies),
                documentation_root=str(root / "docs"),
                service_api=declaration,
            )
        )
        labels.append(label)
        configs[label] = SimpleNamespace(name=package, path=str(root), label=label)
        return root

    yield make
    for label in reversed(labels):
        unregister_app(label)
    forget()


def own(errors, label):
    return [error for error in errors if error.msg.startswith(f"{label}:")]


def test_example_and_valid_fixture_pass(fixture_app):
    fixture_app("svc_provider")
    errors = checks.service_api_checks() + checks.import_boundary_checks()
    assert errors == []


def test_missing_declaration_is_a_warning_for_historical_manifests(fixture_app):
    fixture_app("svc_legacy", service=False, docs={k: v for k, v in DOCS.items() if k != "service-api.md"})
    assert ids(own(checks.service_api_checks(), "svc_legacy"), "skeleton.") == ["skeleton.W018"]
    assert ids(own(checks.record_contract_checks(), "svc_legacy"), "skeleton.E012") == []


def test_declared_service_without_reference_document_fails_e012(fixture_app):
    fixture_app("svc_nodoc", docs={k: v for k, v in DOCS.items() if k != "service-api.md"})
    errors = own(checks.record_contract_checks(), "svc_nodoc")
    assert ids(errors, "skeleton.E012") == ["skeleton.E012"]
    assert "service-api.md" in errors[0].msg


@pytest.mark.parametrize(
    ("source", "fragment"),
    [
        ("class ProviderService:\n    pass\n", "derive from"),
        (
            "from skeleton.service_api import ServiceAPI\n\n"
            "class ProviderService(ServiceAPI):\n"
            "    app_label = 'svc_bad'\n\n"
            "    def total(self, actor):\n        return 0\n",
            "without @service_method",
        ),
        (
            "from skeleton.service_api import ServiceAPI, service_method\n\n"
            "class ProviderService(ServiceAPI):\n"
            "    app_label = 'svc_bad'\n\n"
            "    @service_method(read_only=True, since='1.0')\n"
            "    def total(self, *, actor) -> int:\n        return 0\n",
            "docstring",
        ),
        (VALID_SERVICE.format(label="another_app"), "app_label must be 'svc_bad'"),
        ("OtherName = 1\n", "does not define ProviderService"),
        ("import missing_module_for_fixture\n", "cannot be imported"),
    ],
)
def test_defective_service_api_fails_e018(fixture_app, source, fragment):
    fixture_app("svc_bad", service="custom", files={"service_api.py": source})
    errors = own(checks.service_api_checks(), "svc_bad")
    assert set(ids(errors, "skeleton.")) == {"skeleton.E018"}
    assert any(fragment in error.msg for error in errors), [error.msg for error in errors]


def test_service_module_outside_the_app_package_fails_e018(fixture_app):
    fixture_app("svc_provider")
    fixture_app("svc_outside", service="custom", service_module="svc_fixtures.svc_provider.service_api")
    errors = own(checks.service_api_checks(), "svc_outside")
    assert ids(errors, "skeleton.") == ["skeleton.E018"]
    assert "outside the app package" in errors[0].msg


def test_positional_service_method_fails_at_import(fixture_app):
    source = (
        "from skeleton.service_api import ServiceAPI, service_method\n\n"
        "class ProviderService(ServiceAPI):\n"
        "    @service_method(read_only=True, since='1.0')\n"
        "    def total(self, actor):\n        '''Doc.'''\n"
    )
    fixture_app("svc_positional", service="custom", files={"service_api.py": source})
    errors = own(checks.service_api_checks(), "svc_positional")
    assert ids(errors, "skeleton.") == ["skeleton.E018"]
    assert "keyword-only" in errors[0].msg


def boundary(label):
    return own(checks.import_boundary_checks(), label)


def test_internal_import_of_another_app_fails_e019(fixture_app):
    fixture_app("svc_provider", files={"models.py": "VALUE = 1\n", "services.py": "VALUE = 2\n"})
    fixture_app(
        "svc_consumer",
        dependencies=("svc_provider",),
        files={
            "views.py": "from svc_fixtures.svc_provider.models import VALUE\n",
            "jobs.py": "from svc_fixtures.svc_provider import services\n",
            "relative.py": "from ..svc_provider.models import VALUE\n",
        },
    )
    errors = boundary("svc_consumer")
    assert ids(errors, "skeleton.") == ["skeleton.E019"] * 3
    assert {error.msg.split(":")[1].strip() for error in errors} == {"jobs.py", "relative.py", "views.py"}


def test_declared_service_api_import_passes_and_requires_the_dependency(fixture_app):
    fixture_app("svc_provider")
    consumer = "from svc_fixtures.svc_provider.service_api import ProviderService\n"
    fixture_app("svc_consumer", dependencies=("svc_provider",), files={"integration.py": consumer})
    assert boundary("svc_consumer") == []
    fixture_app("svc_undeclared", files={"integration.py": consumer})
    errors = boundary("svc_undeclared")
    assert ids(errors, "skeleton.") == ["skeleton.E019"]
    assert "does not declare in dependencies" in errors[0].msg


def test_non_public_core_import_is_a_warning(fixture_app):
    fixture_app("svc_core", files={"views.py": "from skeleton.access.services import provision_membership\n"})
    assert ids(boundary("svc_core"), "skeleton.") == ["skeleton.W019"]


@pytest.mark.needs_example_app
def test_importing_notes_internals_from_a_fixture_app_fails(fixture_app):
    notes = get_app("example_notes")
    assert notes.service_api == ServiceAPIDeclaration("examples.notes.service_api", ("NoteService",))
    fixture_app(
        "svc_notes_consumer",
        dependencies=("example_notes",),
        files={
            "ok.py": "from examples.notes.service_api import NoteService\n",
            "bad.py": "from examples.notes.models import Note\n",
            "alias.py": "from applications.notes.models import Note\n",
        },
    )
    errors = boundary("svc_notes_consumer")
    assert sorted(error.msg.split(":")[1].strip() for error in errors) == ["alias.py", "bad.py"]


@pytest.mark.needs_example_app
def test_registry_lists_declared_services(fixture_app):
    from skeleton.service_api.registry import service_registry

    fixture_app("svc_provider")
    registry = service_registry()
    assert [cls.__name__ for cls in registry["svc_provider"]] == ["ProviderService"]
    assert [cls.__name__ for cls in registry["org.skeleton.example-notes"]] == ["NoteService"]


@pytest.mark.needs_example_app
def test_e012_reports_the_missing_document_name(monkeypatch, tmp_path):
    from skeleton.system import app_registry

    manifest = app_registry._manifests["example_notes"]
    monkeypatch.setitem(app_registry._manifests, "example_notes", replace(manifest, documentation_root=str(tmp_path)))
    errors = [error for error in checks.record_contract_checks() if error.id == "skeleton.E012"]
    assert len(errors) == 1 and "spec.md" in errors[0].msg
