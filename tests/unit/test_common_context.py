"""The current context lives in skeleton.common and stays the same object through tenancy (#307)."""

import pytest
from django.apps import apps


def test_tenancy_reexports_the_same_objects():
    from skeleton.common import context as common
    from skeleton.tenancy import context as tenancy

    for name in ("Context", "_context", "current_context", "in_context"):
        assert getattr(tenancy, name) is getattr(common, name)


def test_public_names_stay_importable_from_tenancy():
    from skeleton.tenancy import context

    for name in (
        "InvalidContext",
        "MissingTenantContext",
        "current_context",
        "in_context",
        "memberships_for_user",
        "own_rows",
        "platform_audit_scope",
        "refresh_authenticated_actor",
        "request_cache",
        "request_context",
        "system_context",
        "tenant_context",
        "HTTP_SYSTEM_REASONS",
        "PLATFORM_ADMIN_REASONS",
        "VALIDATED_IDENTITY_REASONS",
        "Context",
        "HasSysid",
        "IdentityRef",
    ):
        assert hasattr(context, name), name


@pytest.mark.parametrize("model", apps.get_models(), ids=lambda model: model._meta.label)
def test_is_tenant_model_matches_the_former_expression(model):
    from skeleton.common.context import is_tenant_model

    assert is_tenant_model(model) == any(field.name == "organization" for field in model._meta.fields)


def test_common_no_longer_imports_tenancy_context():
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "skeleton" / "common"
    for path in root.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom):
                assert node.module != "skeleton.tenancy.context", path.name
