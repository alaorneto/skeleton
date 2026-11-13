"""Names other code imports from the deployment agent survive its split into modules (#317)."""

import importlib

import pytest

pytest.importorskip("fcntl", reason="O agente de deployment exige fcntl (Linux ou macOS).")

pytestmark = pytest.mark.posix

PUBLIC = {
    "deployment_agent.service": ("DeploymentService", "NoRedirect", "APP_ACTIVATION_UPGRADE_CORE"),
    "deployment_agent.releases": (
        "check_drift",
        "check_plan_dependencies",
        "create_release",
        "file_diff",
        "freeze",
        "owned_snapshot",
        "presentation_diff",
        "verified",
        "parse_lock",
        "build_lock",
        "copy_project",
        "resolve_target",
        "dependency_error",
        "ordered_entries",
        "target_django_version",
        "remove_release",
        "wheel_files",
    ),
    "deployment_agent.backends": ("Backend", "ComposeBackend", "backend_for"),
    "deployment_agent.config": ("Config", "read_environment", "effective_runtime_database"),
    "deployment_agent.server": (
        "ControlHandler",
        "StatusHandler",
        "StatusTickets",
        "UnixHTTPServer",
        "serve",
        "log_internal_error",
        "install_signal_handlers",
    ),
    "deployment_agent.storage": (
        "AgentError",
        "Store",
        "atomic_json",
        "atomic_bytes",
        "canonical",
        "deployment_lock",
        "digest",
        "file_hash",
        "identifier",
        "inventory_files",
        "load_json",
        "log_internal_error",
        "private_file",
    ),
}
SERVICE_METHODS = (
    "state",
    "inventory",
    "upload",
    "initialize",
    "ensure_available",
    "preview",
    "prepare",
    "plan_status",
    "submit",
    "public_operation",
    "operation",
    "stage",
    "maintain",
    "execute",
    "finish",
    "health",
    "reconcile_interrupted",
    "recover",
    "prune",
    "catalogue_suggestions",
    "catalogue_sources",
    "fetch_bytes",
    "check_updates",
    "fetch_release",
)


def test_public_names_remain_importable():
    for module_name, names in PUBLIC.items():
        module = importlib.import_module(module_name)
        missing = [name for name in names if not hasattr(module, name)]
        assert missing == [], (module_name, missing)
    service = importlib.import_module("deployment_agent.service").DeploymentService
    assert [name for name in SERVICE_METHODS if not callable(getattr(service, name, None))] == []


def test_reexported_names_are_the_moved_objects():
    from deployment_agent import catalogue, execution, service

    assert service.NoRedirect is catalogue.NoRedirect
    assert service.APP_ACTIVATION_UPGRADE_CORE is execution.APP_ACTIVATION_UPGRADE_CORE
