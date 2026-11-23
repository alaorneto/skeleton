"""``managed_by`` in the configuration fields of a manifest (#702): owner and core floor."""

import json

import pytest

from skeleton.system.packages import (
    PLATFORM_MANAGED_CONFIG_CORE_MINIMUM,
    PackageError,
    canonical_json,
    manifest_template,
    validate_manifest,
)


def manifest(floor, owner):
    value = manifest_template(
        app_id="org.test.managed",
        app_label="project_managed",
        package_name="package_managed",
        publisher="test-publisher",
        app_config_class="ManagedConfig",
        service_classes=["ManagedService"],
    )
    value["compatibility"]["skeleton"] = f">={floor},<1.0.0"
    field = {"name": "region", "type": "str", "default": "sa-east-1"}
    if owner is not None:
        field["managed_by"] = owner
    value["runtime"]["configuration_fields"] = [field]
    return json.loads(canonical_json(value))


def test_managed_by_owner_and_floor(subtests):
    assert PLATFORM_MANAGED_CONFIG_CORE_MINIMUM == "0.6.0"
    accepted = validate_manifest(manifest("0.6.0", "platform"))
    assert accepted["runtime"]["configuration_fields"][0]["managed_by"] == "platform"
    validate_manifest(manifest("0.3.0", "organization"))  # The default owner needs no floor.
    validate_manifest(manifest("0.3.0", None))
    for case, floor, owner, message in (
        ("below the floor", "0.5.0", "platform", "core minimum of 0.6.0"),
        ("unknown owner", "0.6.0", "everyone", "Invalid configuration field owner"),
    ):
        with subtests.test(case=case), pytest.raises(PackageError, match=message):
            validate_manifest(manifest(floor, owner))
