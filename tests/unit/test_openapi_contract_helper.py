"""The contract helper (#179) accepts conforming responses and fails on every kind of divergence."""

from uuid import uuid4

import pytest
from django.http import HttpResponse, JsonResponse

from skeleton.testing.openapi import SchemaMismatch, assert_matches_schema, contract_schema, find_operation

ORGANIZATION = uuid4()
RECORD = f"/api/v1/o/{ORGANIZATION}/tables/example_notes.note/records/{uuid4()}/"


def me(**changes):
    body = {
        "sysid": str(uuid4()),
        "email": "alice@example.test",
        "first_name": "Alice",
        "last_name": "",
        "revision": 1,
        "profile": {
            "sysid": str(uuid4()),
            "revision": 2,
            "display_name": "Alice",
            "timezone": "UTC",
            "language": "pt-br",
        },
    }
    body.update(changes)
    response = JsonResponse(body)
    response["ETag"] = '"1-2"'
    return response


def check(response, method="GET", path="/api/v1/me/"):
    assert_matches_schema(response, method=method, path=path)


def test_a_conforming_response_passes():
    check(me())


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (me(revision="1"), "'1' is not of type 'integer'"),
        (me(unexpected=True), "Additional properties are not allowed"),
        (me(profile={"sysid": None}), "'revision' is a required property"),
        (JsonResponse({"code": "x"}, status=400), "'detail' is a required property"),
    ],
)
def test_a_divergent_body_fails(response, message):
    with pytest.raises(SchemaMismatch, match=message):
        check(response)


def test_a_missing_documented_etag_fails():
    response = me()
    del response["ETag"]
    with pytest.raises(SchemaMismatch, match="ETag"):
        check(response)


def test_undocumented_routes_methods_and_statuses_fail():
    with pytest.raises(SchemaMismatch, match="route not in the OpenAPI schema"):
        check(JsonResponse({}), path="/api/v1/undocumented/")
    with pytest.raises(SchemaMismatch, match="method not in the OpenAPI schema"):
        check(JsonResponse({}), method="PUT")
    with pytest.raises(SchemaMismatch, match="expected application/json"):
        check(HttpResponse("<html></html>"))


def test_errors_of_unknown_routes_still_follow_the_error_shape():
    check(JsonResponse({"code": "not_found", "detail": "Not found."}, status=404), path="/api/v1/undocumented/")
    with pytest.raises(SchemaMismatch, match="detail"):
        check(JsonResponse({"code": "not_found"}, status=404), path="/api/v1/undocumented/")


def test_responses_outside_the_api_are_ignored():
    check(HttpResponse("anything"), path="/o/page/")


def test_records_resolve_to_the_table_template_and_204_has_no_body():
    template, operation = find_operation(contract_schema(), "DELETE", RECORD)
    assert template == "/api/v1/o/{organization_id}/tables/example_notes.note/records/{sysid}/"
    assert operation is not None and "204" in operation["responses"]
    check(HttpResponse(status=204), method="DELETE", path=RECORD)
