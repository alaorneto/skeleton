"""OpenAPI generator (#178): field types from _meta, sensitive fields and the full document."""

import json
from pathlib import Path

import pytest
from django.db import models

from skeleton.api.openapi import (
    UNDOCUMENTED,
    build_schema,
    field_schema,
    full_audience,
    internal_type_schema,
    nullable,
    record_input_schema,
    record_schema,
)

META_SCHEMA = Path(__file__).resolve().parents[1] / "fixtures" / "openapi" / "oas-3.1-schema-2022-10-07.json"


@pytest.fixture(scope="module")
def document():
    return build_schema(full_audience())


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        (models.CharField(max_length=12), {"type": "string", "maxLength": 12}),
        (models.TextField(), {"type": "string"}),
        (models.BooleanField(), {"type": "boolean"}),
        (models.IntegerField(), {"type": "integer"}),
        (models.PositiveBigIntegerField(), {"type": "integer", "minimum": 0}),
        (models.DecimalField(max_digits=5, decimal_places=2), {"type": "number"}),
        (models.UUIDField(), {"type": "string", "format": "uuid"}),
        (models.DateTimeField(), {"type": "string", "format": "date-time"}),
        (models.DateField(), {"type": "string", "format": "date"}),
        (models.EmailField(), {"type": "string", "maxLength": 254}),  # Internally a CharField.
        (models.JSONField(), {}),
        (models.DateTimeField(null=True), {"type": ["string", "null"], "format": "date-time"}),
        (
            models.CharField(max_length=8, choices=[("a", "A"), ("b", "B")]),
            {"type": "string", "maxLength": 8, "enum": ["a", "b"]},
        ),
    ],
)
def test_field_types_come_from_the_internal_type(field, expected):
    assert field_schema(field) == expected


def test_unknown_internal_types_are_documented_as_any_value():
    assert internal_type_schema("GeometryField") == {}


def test_nullable_keeps_enums_and_references():
    assert nullable({"type": "string", "enum": ["a"]}) == {"type": ["string", "null"], "enum": ["a", None]}
    assert nullable({"$ref": "#/components/schemas/X"}) == {
        "anyOf": [{"$ref": "#/components/schemas/X"}, {"type": "null"}]
    }


def test_relations_are_uuids_and_hidden_references_accept_null():
    from examples.notes.models import Note

    owner = Note._meta.get_field("owner")
    assert field_schema(owner) == {"type": "string", "format": "uuid"}
    assert field_schema(owner, hidden_references=True) == {"type": ["string", "null"], "format": "uuid"}
    assert field_schema(Note._meta.get_field("visibility"))["enum"] == ["private", "group", "organization"]


def test_record_schemas_never_name_sensitive_fields():
    from skeleton.system.registry import get_spec

    spec = get_spec("example_notes.note")
    assert "body" in spec.sensitive_fields and "body" in spec.read_fields and "body" in spec.write_fields
    record = record_schema(spec)
    assert "body" not in record["properties"] and "body" not in record["required"]
    assert "additionalProperties" not in record  # A rendered sensitive field keeps the object open.
    created = record_input_schema(spec, creating=True)
    assert set(created["properties"]) == {"code", "title"}
    assert "body" not in json.dumps(created)
    assert "required" not in record_input_schema(spec, creating=False)


def test_the_full_document_is_valid_openapi_3_1(document):
    from jsonschema import Draft202012Validator

    meta = json.loads(META_SCHEMA.read_text(encoding="utf-8"))
    Draft202012Validator(meta).validate(document)


def test_every_core_route_is_documented_with_unique_operation_ids(document):
    operations = [operation for item in document["paths"].values() for operation in item.values()]
    assert not [operation["operationId"] for operation in operations if operation.get(UNDOCUMENTED)]
    ids = [operation["operationId"] for operation in operations]
    assert len(ids) == len(set(ids))
    for path in (
        "/api/v1/notifications/",
        "/api/v1/me/notification-preferences/",
        "/api/v1/o/{organization_id}/apps/{app_label}/configuration/",
        "/api/v1/o/{organization_id}/webhooks/{sysid}/rotate/",
        "/api/v1/me/tokens/",
        "/api/v1/o/{organization_id}/tables/example_notes.note/records/{sysid}/attachments/",
        "/api/v1/openapi.json",
    ):
        assert path in document["paths"], path


def test_no_component_or_path_names_a_sensitive_field(document):
    from skeleton.api.serializers import DEDICATED
    from skeleton.system.registry import all_specs, get_spec

    schemas = document["components"]["schemas"]
    for spec in all_specs():
        if not spec.expose_api:
            continue
        key = spec.model._meta.label_lower
        for name in (f"Record.{key}", f"RecordInput.{key}.create", f"RecordInput.{key}.update"):
            assert not set(spec.sensitive_fields) & set(schemas[name]["properties"]), name
        for path, item in document["paths"].items():
            if f"/tables/{key}/" not in path:
                continue
            for field in spec.sensitive_fields:
                assert f"/references/{field}/" not in path
                for operation in item.values():
                    assert field not in {parameter.get("name") for parameter in operation["parameters"]}
    for resource, (label, _) in DEDICATED.items():
        sensitive = set(get_spec(label.lower()).sensitive_fields)
        assert not sensitive & set(schemas[f"Dedicated.{resource}"]["properties"]), resource


def test_if_match_operations_document_412_and_428(document):
    patch = document["paths"]["/api/v1/o/{organization_id}/tables/example_notes.note/records/{sysid}/"]["patch"]
    assert {"$ref": "#/components/parameters/IfMatch"} in patch["parameters"]
    assert patch["responses"]["412"] == {"$ref": "#/components/responses/PreconditionFailed"}
    assert patch["responses"]["428"] == {"$ref": "#/components/responses/PreconditionRequired"}
    assert patch["responses"]["200"]["headers"]["ETag"] == {"$ref": "#/components/headers/ETag"}
    listing = document["paths"]["/api/v1/o/{organization_id}/tables/example_notes.note/records/"]["get"]
    assert {"$ref": "#/components/parameters/Page"} in listing["parameters"]
    assert listing["responses"]["200"]["content"]["application/json"]["schema"]["allOf"][0] == {
        "$ref": "#/components/schemas/Page"
    }
