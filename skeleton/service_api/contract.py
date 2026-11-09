"""Static contract of a service class, shared by check E018 and the test kit (docs/service-api.md).

``structural_problems`` is what the system check enforces on every registered app: the class
derives from ``ServiceAPI``, is instantiable without arguments, publishes at least one method,
and every public method is a documented ``@service_method`` with keyword-only arguments.
``contract_problems`` adds what ``assert_service_contract`` enforces in tests: resolvable return
annotations limited to DTOs, ``Page``, tuples and primitives, annotated parameters and the
docstring sections Autorização, Erros and Efeitos.
"""

import dataclasses
import enum
import inspect
import re
import sys
import types
import typing
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from .base import SPEC_ATTRIBUTE, ServiceAPI, ServiceMethodSpec
from .dto import Page

PRIMITIVES: frozenset[type] = frozenset({type(None), bool, int, str, Decimal, date, datetime, UUID})
DOCSTRING_SECTIONS = ("Autorização", "Erros", "Efeitos")
_BASE_MEMBERS = frozenset(dir(ServiceAPI))
_MAX_DEPTH = 8


def _public_members(service_class: type) -> dict[str, Any]:
    members: dict[str, Any] = {}
    for klass in reversed(service_class.__mro__):
        if klass in (object, ServiceAPI):
            continue
        for name, value in vars(klass).items():
            if not name.startswith("_") and name not in _BASE_MEMBERS:
                members[name] = value
    return members


def _signature_problems(name: str, function: Callable[..., Any]) -> list[str]:
    try:
        parameters = list(inspect.signature(function).parameters.values())
    except (TypeError, ValueError):
        return [f"{name}: signature cannot be inspected."]
    if not parameters or parameters[0].name != "self":
        return [f"{name}: service methods are instance methods."]
    problems = []
    for parameter in parameters[1:]:
        if parameter.kind is not inspect.Parameter.KEYWORD_ONLY:
            problems.append(f"{name}: parameter '{parameter.name}' must be keyword-only.")
    if not any(parameter.name == "actor" for parameter in parameters[1:]):
        problems.append(f"{name}: service methods receive the explicit keyword 'actor'.")
    return problems


def structural_problems(service_class: object) -> list[str]:
    """Problems the system check E018 reports for a declared service class."""
    if not isinstance(service_class, type) or not issubclass(service_class, ServiceAPI):
        return ["the class must derive from skeleton.service_api.ServiceAPI."]
    problems: list[str] = []
    try:
        service_class()
    except Exception as exc:  # Any failure means consumers cannot instantiate it.
        problems.append(f"consumers instantiate it without arguments ({type(exc).__name__}).")
    methods = 0
    for name, member in sorted(_public_members(service_class).items()):
        if isinstance(member, staticmethod | classmethod | property) or not callable(member):
            if isinstance(member, staticmethod | classmethod | property):
                problems.append(f"{name}: public members are @service_method instance methods.")
            continue
        if not isinstance(getattr(member, SPEC_ATTRIBUTE, None), ServiceMethodSpec):
            problems.append(f"{name}: public method without @service_method; prefix internal helpers with '_'.")
            continue
        methods += 1
        function = inspect.unwrap(member)
        problems.extend(_signature_problems(name, function))
        if not (inspect.getdoc(function) or "").strip():
            problems.append(f"{name}: service methods require a docstring.")
    if methods == 0:
        problems.append("the class publishes no @service_method.")
    return problems


def _raw_annotations(function: Callable[..., Any]) -> dict[str, Any]:
    if sys.version_info >= (3, 14):
        import annotationlib  # Lazy annotations may name TYPE_CHECKING-only types.

        return dict(annotationlib.get_annotations(function, format=annotationlib.Format.FORWARDREF))
    return dict(getattr(function, "__annotations__", {}))


def _resolve(annotation: Any, function: Callable[..., Any]) -> Any:
    holder = types.SimpleNamespace(__annotations__={"value": annotation})
    return typing.get_type_hints(holder, globalns=getattr(function, "__globals__", {}))["value"]


def _type_problem(annotation: Any, *, seen: set[type], depth: int = 0) -> str | None:
    """Why an annotation may not cross the boundary, or None when it is a valid result type."""
    if depth > _MAX_DEPTH:
        return "type is nested too deeply"
    if annotation is None or annotation in PRIMITIVES:
        return None
    origin = typing.get_origin(annotation)
    arguments = typing.get_args(annotation)
    if origin is typing.Union or origin is types.UnionType:
        problems = [_type_problem(item, seen=seen, depth=depth + 1) for item in arguments]
        return next((problem for problem in problems if problem), None)
    if origin is typing.Literal:
        if all(type(item) in (str, int, bool) for item in arguments):
            return None
        return "Literal values must be str, int or bool"
    if origin is tuple:
        items = [item for item in arguments if item is not Ellipsis]
        if not items:
            return "tuple requires item types"
        problems = [_type_problem(item, seen=seen, depth=depth + 1) for item in items]
        return next((problem for problem in problems if problem), None)
    if origin is Page:
        if len(arguments) != 1:
            return "Page requires its item type (Page[T])"
        return _type_problem(arguments[0], seen=seen, depth=depth + 1)
    if annotation is Page:
        return "Page requires its item type (Page[T])"
    if origin is not None:
        return f"{annotation!r} is not a DTO, Page, tuple or primitive"
    if isinstance(annotation, type) and issubclass(annotation, enum.Enum):
        return None if issubclass(annotation, str) else f"{annotation.__name__} must be a text enum"
    if isinstance(annotation, type) and dataclasses.is_dataclass(annotation):
        if not annotation.__dataclass_params__.frozen:  # type: ignore[attr-defined]
            return f"{annotation.__name__} must be @dataclass(frozen=True)"
        if annotation in seen:
            return None
        seen.add(annotation)
        try:
            fields = typing.get_type_hints(annotation)
        except Exception as exc:  # Unresolvable field annotations are reported.
            return f"{annotation.__name__} field annotations cannot be resolved ({type(exc).__name__})"
        for field in dataclasses.fields(annotation):
            problem = _type_problem(fields.get(field.name), seen=seen, depth=depth + 1)
            if problem:
                return f"{annotation.__name__}.{field.name}: {problem}"
        return None
    name = getattr(annotation, "__qualname__", None) or repr(annotation)
    return f"{name} is not a DTO, Page, tuple or primitive"


def _docstring_problems(name: str, function: Callable[..., Any]) -> list[str]:
    text = inspect.getdoc(function) or ""
    return [
        f"{name}: docstring lacks the '{section}:' section."
        for section in DOCSTRING_SECTIONS
        if not re.search(rf"^\s*{section}\s*:", text, re.MULTILINE)
    ]


def _annotation_problems(name: str, function: Callable[..., Any]) -> list[str]:
    raw = _raw_annotations(function)
    problems = []
    for parameter in list(inspect.signature(function).parameters.values())[1:]:
        if parameter.name != "actor" and parameter.name not in raw:
            problems.append(f"{name}: parameter '{parameter.name}' requires a type annotation.")
    if "return" not in raw:
        return [*problems, f"{name}: the return type must be annotated."]
    try:
        resolved = _resolve(raw["return"], function)
    except Exception as exc:  # Reported instead of failing on import details.
        return [*problems, f"{name}: the return annotation cannot be resolved ({type(exc).__name__})."]
    problem = _type_problem(resolved, seen=set())
    if problem:
        problems.append(f"{name}: return type {problem}.")
    return problems


def contract_problems(service_class: object) -> list[str]:
    """Every problem ``assert_service_contract`` reports: structure, types and documentation."""
    problems = structural_problems(service_class)
    if not isinstance(service_class, type) or not issubclass(service_class, ServiceAPI):
        return problems
    for name in sorted(service_class.service_methods()):
        function = inspect.unwrap(getattr(service_class, name))
        problems.extend(_annotation_problems(name, function))
        problems.extend(_docstring_problems(name, function))
    return problems
