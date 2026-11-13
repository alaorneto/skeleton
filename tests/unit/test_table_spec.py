"""TableSpec keeps field lists as tuples of names, and the system field lists have one source (#310)."""

import dataclasses

import pytest

from skeleton.system.registry import TableSpec


class Model:
    """A stand-in: TableSpec does not inspect the model when it is built."""


def spec(**values):
    return TableSpec(model=Model, description="x", scope="global", **values)


def test_tablespec_normalizes_lists_to_tuples():
    built = spec(read_fields=["a", "b"], write_fields=["a"], sensitive_fields=["b"], sequence_fields=[("a", "k")])
    assert built.read_fields == ("a", "b") and built.write_fields == ("a",) and built.sensitive_fields == ("b",)
    assert built.sequence_fields == (("a", "k"),)


@pytest.mark.parametrize("name", ["read_fields", "write_fields", "sensitive_fields", "sequence_fields"])
def test_tablespec_rejects_bare_string(name):
    with pytest.raises(TypeError, match=name):
        spec(**{name: "title"})


@pytest.mark.parametrize("name", ["read_fields", "write_fields", "sensitive_fields"])
def test_tablespec_rejects_non_string_field_names(name):
    with pytest.raises(TypeError, match=name):
        spec(**{name: (1,)})


def test_replace_keeps_normalized_tuples():
    built = dataclasses.replace(spec(read_fields=["a"]), write_fields=["a", "b"])
    assert built.read_fields == ("a",) and built.write_fields == ("a", "b")


def test_system_field_constants_are_single_source():
    from skeleton.common.models import ACCESS_FIELD_NAMES, SYSTEM_FIELD_NAMES
    from skeleton.records.services import ACCESS_FIELDS, SYSTEM_FIELDS
    from skeleton.system.presentation import SYSTEM_READ_FIELDS
    from skeleton.web import services as web_services

    assert SYSTEM_FIELDS is SYSTEM_FIELD_NAMES and ACCESS_FIELDS is ACCESS_FIELD_NAMES
    assert web_services.SYSTEM_FIELDS is SYSTEM_FIELD_NAMES and web_services.ACCESS_FIELDS is ACCESS_FIELD_NAMES
    assert SYSTEM_READ_FIELDS == frozenset(SYSTEM_FIELD_NAMES)
    assert SYSTEM_FIELD_NAMES == (
        "sysid",
        "created_at",
        "updated_at",
        "created_by",
        "updated_by",
        "deleted_at",
        "deleted_by",
        "revision",
    )
