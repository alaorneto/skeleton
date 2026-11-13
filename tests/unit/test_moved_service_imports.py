"""The services moved to their owning packages stay importable from the old path (#303)."""


def test_legacy_import_paths_resolve_to_the_moved_functions():
    from skeleton.access import administration
    from skeleton.accounts import services as legacy
    from skeleton.tenancy import services as tenancy_services

    assert legacy.create_organization is tenancy_services.create_organization
    assert legacy.update_organization is tenancy_services.update_organization
    for name in (
        "AdministratorSummary",
        "OrganizationOverview",
        "organization_overview",
        "designate_organization_admin",
    ):
        assert getattr(legacy, name) is getattr(administration, name)


def test_accounts_no_longer_reaches_into_private_access_names():
    import ast
    from pathlib import Path

    source = (Path(__file__).resolve().parents[2] / "skeleton" / "accounts" / "services.py").read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module == "skeleton.access.services":
            assert not [alias.name for alias in node.names if alias.name.startswith("_")]
