"""Contract test kit for service APIs (docs/service-api.md, "Testes obrigatórios").

The helpers are plain functions that raise ``AssertionError``: they work with pytest or unittest
and in PostgreSQL with the runtime role, because they only call public services.

- ``assert_service_contract(ServiceClass)``: keyword-only signatures, DTO/``Page``/primitive
  return annotations and docstrings with Autorização, Erros and Efeitos.
- ``build_service_scenario(Model)``: organizations A/B, an actor allowed in both, an actor denied
  in A, an outsider, records created in each organization and the app disabled on demand.
- ``assert_tenant_isolated``, ``assert_denied_as_not_found``, ``assert_unavailable_when_disabled``,
  ``assert_cas_conflict`` and ``assert_rejects_other_actor`` prove the security properties.

The scenario writes committed rows through the public identity and provisioning services; use it
inside the project's isolated test database fixture, never against a shared database.
"""

import dataclasses
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from skeleton.service_api.contract import contract_problems
from skeleton.service_api.errors import Conflict, Forbidden, NotFound, ServiceError, Unavailable

__all__ = [
    "ServiceScenario",
    "assert_cas_conflict",
    "assert_denied_as_not_found",
    "assert_rejects_other_actor",
    "assert_service_contract",
    "assert_tenant_isolated",
    "assert_unavailable_when_disabled",
    "build_service_scenario",
]

FIXTURE_PASSWORD = "Service-contract-fixture-2026!"  # Disposable test identities only.
_MAX_DEPTH = 16


def assert_service_contract(service_class: object) -> None:
    """Fail with every contract problem of a service class at once."""
    problems = contract_problems(service_class)
    if problems:
        name = getattr(service_class, "__qualname__", repr(service_class))
        raise AssertionError(f"{name} violates the service API contract:\n- " + "\n- ".join(problems))


def _expect(error: type[ServiceError], call: Callable[[], object], what: str) -> ServiceError:
    try:
        result = call()
    except error as exc:
        return exc
    except ServiceError as exc:
        raise AssertionError(f"{what}: expected {error.__name__}, got {type(exc).__name__} ({exc.code}).") from exc
    raise AssertionError(f"{what}: expected {error.__name__}, the call returned {result!r}.")


def assert_denied_as_not_found(call: Callable[[], object], *, missing: Callable[[], object] | None = None) -> None:
    """The denied call raises NotFound, indistinguishable from a missing target when given."""
    denied = _expect(NotFound, call, "denied call")
    if missing is not None:
        absent = _expect(NotFound, missing, "missing target")
        if (denied.code, denied.detail) != (absent.code, absent.detail):
            raise AssertionError("A denied target answers differently from a missing one, revealing it exists.")


def assert_unavailable_when_disabled(call: Callable[[], object], *, disabled: Callable[[], Any]) -> None:
    """Inside ``disabled()`` (a context manager such as ``scenario.application_disabled``) the call is Unavailable.

    ``call`` opens its own context (``scenario.caller``): the app is disabled by an administrator first.
    """
    with disabled():
        _expect(Unavailable, call, "call with the provider app disabled")


def assert_rejects_other_actor(call: Callable[[], object]) -> None:
    """A call whose ``actor`` differs from the context actor is Forbidden."""
    _expect(Forbidden, call, "call with an actor other than the context")


def assert_cas_conflict(first: Callable[[], object], second: Callable[[], object]) -> object:
    """Two writes with the same expected revision: the first wins, the second is Conflict(stale_revision)."""
    result = first()
    conflict = _expect(Conflict, second, "second write with the same revision")
    if conflict.code != "stale_revision":
        raise AssertionError(f"Expected Conflict(code='stale_revision'), got code {conflict.code!r}.")
    return result


def _markers(foreign: Iterable[object]) -> tuple[set[UUID], set[str]]:
    identifiers: set[UUID] = set()
    texts: set[str] = set()
    for item in foreign:
        value = getattr(item, "pk", item)
        if isinstance(value, UUID):
            identifiers.add(value)
            texts.add(str(value))
        elif isinstance(value, str) and value:
            texts.add(value)
        else:
            raise TypeError("foreign markers are UUIDs, records or nonempty strings.")
    return identifiers, texts


def _leaks(value: object, identifiers: set[UUID], texts: set[str], depth: int = 0) -> str | None:
    if depth > _MAX_DEPTH:
        return None
    if isinstance(value, UUID):
        return str(value) if value in identifiers else None
    if isinstance(value, str):
        return next((text for text in texts if text in value), None)
    if isinstance(value, tuple | frozenset | list | set):
        return next((leak for item in value if (leak := _leaks(item, identifiers, texts, depth + 1))), None)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        for field in dataclasses.fields(value):
            leak = _leaks(getattr(value, field.name), identifiers, texts, depth + 1)
            if leak:
                return leak
    return None


def assert_tenant_isolated(call: Callable[[], object], *, foreign: Iterable[object]) -> object:
    """Nothing of the other organization appears in the result, page, count or error.

    ``foreign`` lists records, UUIDs or distinctive strings (codes, titles) created in the other
    organization. An error is accepted only when it is a service error that reveals none of them.
    """
    identifiers, texts = _markers(foreign)
    try:
        result = call()
    except ServiceError as exc:
        messages = [exc.detail, exc.code, *getattr(exc, "errors", {}).keys()]
        messages += [message for values in getattr(exc, "errors", {}).values() for message in values]
        leak = _leaks(tuple(str(item) for item in messages), identifiers, texts)
        if leak:
            raise AssertionError(f"The error reveals data of another organization: {leak!r}.") from exc
        return exc
    leak = _leaks(result, identifiers, texts)
    if leak:
        raise AssertionError(f"The result leaks data of another organization: {leak!r}.")
    return result


@contextmanager
def acting_as(user: Any, organization: Any, *, source: str = "test:service-api") -> Iterator[Any]:
    """Open the request and tenant context of user in organization and yield the actor."""
    from skeleton.tenancy.context import request_context, tenant_context

    with request_context(actor=user, request_id=uuid4(), source=source):
        with tenant_context(organization_id=organization.pk, actor=user, source=source):
            yield user


@dataclass
class ServiceScenario:
    """Organizations A/B and actors for one SecuredRecord model of the app under test.

    - ``member``: member of A and B with TablePermission on every action of the table in both.
    - ``denied``: member of A with no TablePermission on the table (default deny).
    - ``outsider``: active identity without membership.
    - ``admin_a``/``admin_b``: organization owners, used to configure the app.
    """

    model: Any
    app_label: str
    a: Any
    b: Any
    admin_a: Any
    admin_b: Any
    member: Any
    denied: Any
    outsider: Any

    def acting(self, user: Any, organization: Any | None = None) -> Any:
        return acting_as(user, organization or self.a)

    def as_member(self, organization: Any | None = None) -> Any:
        return acting_as(self.member, organization or self.a)

    def as_denied(self) -> Any:
        return acting_as(self.denied, self.a)

    def create(
        self, organization: Any, *, owner: Any | None = None, visibility: str = "organization", **values: Any
    ) -> Any:
        """Persist one record in organization (system context of fixtures, never of the service)."""
        from skeleton.tenancy.context import system_context

        with system_context(reason="fixture:service-api-record", organization_id=organization.pk):
            record = self.model(organization=organization, owner=owner or self.member, visibility=visibility, **values)
            record.save()
        return record

    def caller(
        self, function: Callable[[Any], object], *, user: Any | None = None, organization: Any | None = None
    ) -> Callable[[], object]:
        """A zero-argument call of function(actor) inside its own context (member in A by default).

        Contexts cannot nest or switch actors, so every assertion helper receives such callables.
        """

        def call() -> object:
            with acting_as(user or self.member, organization or self.a) as actor:
                return function(actor)

        return call

    @contextmanager
    def application_disabled(self, organization: Any | None = None) -> Iterator[None]:
        """Disable the app (and its dependents) in organization, A by default, and enable it again after."""
        organization = organization or self.a
        admin = self.admin_a if organization.pk == self.a.pk else self.admin_b
        with acting_as(admin, organization):
            _set_enabled(self.app_label, admin, enabled=False)
        try:
            yield
        finally:
            with acting_as(admin, organization):
                _set_enabled(self.app_label, admin, enabled=True)


def _set_enabled(app_label: str, admin: Any, *, enabled: bool) -> None:
    from skeleton.system.app_registry import dependent_labels, required_labels
    from skeleton.system.applications import application_state, configure_application

    order = [*required_labels(app_label), app_label] if enabled else [*dependent_labels(app_label), app_label]
    for label in order:
        state = application_state(label)
        if state["enabled"] != enabled:
            configure_application(
                label, user=admin, enabled=enabled, configuration=state["configuration"], revision=state["revision"]
            )


def build_service_scenario(model: Any, *, slug: str = "service-contract") -> ServiceScenario:
    """Create identities, organizations A/B, grants and enable the app of model in both."""
    from allauth.account.models import EmailAddress  # type: ignore[import-untyped]

    from skeleton.access.models import Action, Role, TablePermission
    from skeleton.access.services import provision_membership
    from skeleton.accounts.services import create_identity, create_organization
    from skeleton.system.models import TableDefinition
    from skeleton.system.synchronization import sync_table_catalog
    from skeleton.tenancy.context import system_context

    users = {}
    with system_context(reason="fixture:service-api-identities"):
        for name in ("admin-a", "admin-b", "member", "denied", "outsider"):
            user = create_identity(email=f"{name}@{slug}.example.test", password=FIXTURE_PASSWORD)
            EmailAddress.objects.create(user=user, email=user.email, verified=True, primary=True)
            users[name] = user
    a = create_organization(owner=users["admin-a"], slug=f"{slug}-a", name="Service contract A")
    b = create_organization(owner=users["admin-b"], slug=f"{slug}-b", name="Service contract B")
    sync_table_catalog()
    table = TableDefinition.objects.get(model_label=model._meta.label_lower)
    for organization in (a, b):
        with system_context(reason="fixture:service-api-grants", organization_id=organization.pk):
            builtin = Role.objects.get(code="member")
            contract = Role(organization=organization, code="service-contract", name="Service contract")
            contract.save()
            for action in Action.values:
                TablePermission(organization=organization, role=contract, table=table, action=action).save()
            provision_membership(organization=organization, user=users["member"], role=builtin)
            provision_membership(organization=organization, user=users["member"], role=contract)
            if organization.pk == a.pk:
                provision_membership(organization=organization, user=users["denied"], role=builtin)
    for organization, admin in ((a, users["admin-a"]), (b, users["admin-b"])):
        with acting_as(admin, organization):
            _set_enabled(model._meta.app_label, admin, enabled=True)
    return ServiceScenario(
        model=model,
        app_label=model._meta.app_label,
        a=a,
        b=b,
        admin_a=users["admin-a"],
        admin_b=users["admin-b"],
        member=users["member"],
        denied=users["denied"],
        outsider=users["outsider"],
    )
