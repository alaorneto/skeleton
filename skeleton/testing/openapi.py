"""Validate real ``/api/v1/`` responses against the published OpenAPI schema (#179).

The schema is the same document ``GET /api/v1/openapi.json`` serves
(:func:`skeleton.api.openapi.build_schema`), built with
:func:`~skeleton.api.openapi.full_audience`, so it describes every table and operation of the
URL tree; the shapes do not depend on who asks. Use it in tests only: it needs ``jsonschema``,
a development dependency.

- :func:`assert_matches_schema` checks one response: the route and method must be documented,
  the status must be one of the operation's responses (or its ``default``), the JSON body must
  validate, and a documented ``ETag`` must be present. Error responses of an undocumented route
  or method still have to be ``{code, detail, fields}``.
- :func:`checked_client_responses` wraps ``django.test.Client`` (and DRF's ``APIClient``) so every
  API response of a block is checked; the core suite enables it for every test in
  ``tests/conftest.py``.

A divergence raises :class:`SchemaMismatch` (an ``AssertionError``) naming the operation, the
status and the JSON path that failed.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from urllib.parse import quote

API_PREFIX = "/api/v1/"
_BASE = "urn:skeleton:openapi"
_cache: dict[Any, dict[str, Any]] = {}
_validators: dict[tuple[int, str], Any] = {}


class SchemaMismatch(AssertionError):
    """A response diverges from the published OpenAPI schema."""


def _cache_key(urlconf: str | None) -> Any:
    from django.urls import get_resolver

    from skeleton.system.app_registry import all_apps
    from skeleton.system.registry import all_specs

    # Specs are frozen and hashable: a test that replaces one (an attachment policy) gets a new schema.
    return (id(get_resolver(urlconf)), all_specs(), tuple(manifest.app_label for manifest in all_apps()))


def contract_schema(urlconf: str | None = None) -> dict[str, Any]:
    """The full OpenAPI document of the current URL tree and registry (cached while they hold)."""
    from skeleton.api.openapi import build_schema, full_audience

    key = _cache_key(urlconf)
    schema = _cache.get(key)
    if schema is None:
        _cache.clear()
        _validators.clear()
        schema = _cache[key] = build_schema(full_audience(), urlconf=urlconf)
    return schema


def _template_pattern(template: str) -> re.Pattern[str]:
    parts = re.split(r"({\w+})", template)
    return re.compile("".join("[^/]+" if part.startswith("{") else re.escape(part) for part in parts) + "$")


def find_operation(schema: dict[str, Any], method: str, path: str) -> tuple[str | None, dict[str, Any] | None]:
    """``(path template, operation)`` for a concrete path; the most literal template wins."""
    matches = [template for template in schema["paths"] if _template_pattern(template).match(path)]
    if not matches:
        return None, None
    template = max(matches, key=lambda item: len(re.sub(r"{\w+}", "", item)))
    return template, schema["paths"][template].get(method.lower())


def _pointer(*parts: str) -> str:
    return "#" + "".join("/" + quote(part.replace("~", "~0").replace("/", "~1"), safe="") for part in parts)


def _validator(schema: dict[str, Any], pointer: str) -> Any:
    try:
        from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
        from referencing import Registry, Resource
        from referencing.jsonschema import DRAFT202012
    except ImportError as exc:  # pragma: no cover - a development dependency
        raise RuntimeError("skeleton.testing.openapi needs jsonschema (requirements-dev).") from exc

    key = (id(schema), pointer)
    validator = _validators.get(key)
    if validator is None:
        resource = Resource.from_contents(schema, default_specification=DRAFT202012)
        registry = Registry().with_resource(_BASE, resource)
        validator = Draft202012Validator(
            {"$ref": _BASE + pointer}, registry=registry, format_checker=Draft202012Validator.FORMAT_CHECKER
        )
        _validators[key] = validator
    return validator


def _resolve_response(schema: dict[str, Any], response: dict[str, Any]) -> tuple[dict[str, Any], tuple[str, ...]]:
    """The response object and its location, following one ``$ref`` into components."""
    reference = response.get("$ref")
    if reference is None:
        return response, ()
    name = reference.rsplit("/", 1)[-1]
    return schema["components"]["responses"][name], ("components", "responses", name)


def _json(response: Any) -> tuple[bool, Any]:
    content_type = response.get("Content-Type", "") if hasattr(response, "get") else ""
    if "json" not in content_type:
        return False, None
    raw = getattr(response, "content", b"")
    if not raw:
        return True, None
    return True, json.loads(raw)


def _check(schema: dict[str, Any], pointer: str, body: Any, label: str) -> None:
    errors = sorted(_validator(schema, pointer).iter_errors(body), key=lambda error: list(error.absolute_path))
    if errors:
        first = errors[0]
        where = "$" + "".join(f"[{item!r}]" for item in first.absolute_path)
        raise SchemaMismatch(f"{label}: {where}: {first.message}")


def _check_error(schema: dict[str, Any], response: Any, label: str) -> None:
    is_json, body = _json(response)
    if is_json and body is not None:
        _check(schema, _pointer("components", "schemas", "Error"), body, label)


def assert_matches_schema(
    response: Any, *, method: str | None = None, path: str | None = None, schema: dict[str, Any] | None = None
) -> None:
    """Fail when an ``/api/v1/`` response diverges from the schema; other paths are ignored."""
    request = getattr(response, "request", None) or {}
    method = (method or request.get("REQUEST_METHOD", "GET")).upper()
    path = path or request.get("PATH_INFO", "")
    if not path.startswith(API_PREFIX) or method in {"HEAD", "OPTIONS"}:
        return
    schema = schema if schema is not None else contract_schema()
    status = response.status_code
    label = f"{method} {path} → {status}"
    template, operation = find_operation(schema, method, path)
    if operation is None:
        if status >= 400:
            _check_error(schema, response, label)
            return
        raise SchemaMismatch(f"{label}: {'method' if template else 'route'} not in the OpenAPI schema.")
    responses = operation["responses"]
    key = next((item for item in (str(status), f"{status // 100}XX", "default") if item in responses), None)
    if key is None:
        raise SchemaMismatch(f"{label}: status not documented ({', '.join(responses)}).")
    documented, location = _resolve_response(schema, responses[key])
    if not location:
        location = ("paths", template or "", method.lower(), "responses", key)
    label = f"{label} ({operation['operationId']})"
    content = documented.get("content", {}).get("application/json")
    if content is not None and status != 204:
        is_json, body = _json(response)
        if not is_json:
            if status >= 400:
                return  # A non-JSON error (a framework page) is outside the JSON contract.
            raise SchemaMismatch(f"{label}: expected application/json, got {response.get('Content-Type')!r}.")
        _check(schema, _pointer(*location, "content", "application/json", "schema"), body, label)
    if "ETag" in documented.get("headers", {}) and not response.has_header("ETag"):
        raise SchemaMismatch(f"{label}: the documented ETag header is missing.")


@contextmanager
def checked_client_responses() -> Iterator[None]:
    """Check every ``/api/v1/`` response the Django test client returns inside the block."""
    from django.test.client import ClientHandler

    original = ClientHandler.__call__

    def checked(self: Any, environ: dict[str, Any]) -> Any:
        response = original(self, environ)
        assert_matches_schema(response, method=environ.get("REQUEST_METHOD"), path=environ.get("PATH_INFO"))
        return response

    ClientHandler.__call__ = checked  # type: ignore[method-assign]
    try:
        yield
    finally:
        ClientHandler.__call__ = original  # type: ignore[method-assign]
