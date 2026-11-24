"""Open and activate apps in tests, now that installed apps are closed by default (#22).

Availability and activation default closed: a fresh test database has no app that any
organization can use. Tests of an app's behavior first state the precondition an
operator would create, with these helpers, instead of relying on the manifest.

A first-party library (#792) has no availability nor activation: it is effective while an
effective app depends on it. Inside ``library_consumer()`` (and so inside ``apps_open_by_default()``
when libraries are installed) a test app that depends on every installed library is registered;
``activate_app(<library>, organization)`` activates that consumer, and ``open_app(<library>)``
writes nothing.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from typing import Any
from uuid import UUID, uuid4

REASON = "test:app-activation"

# The test app that depends on every installed library inside library_consumer() (#792).
LIBRARY_CONSUMER = "test_library_consumer"


def open_app(app_label: str, *, using: str = "default") -> None:
    """Make an installed app available to every organization (``mode=all``); a library has no gate."""
    from django.db import transaction

    from skeleton.system.app_registry import is_library
    from skeleton.system.models import AppAvailability
    from skeleton.tenancy.context import system_context

    if is_library(app_label):
        return

    with transaction.atomic(using=using), system_context(reason=REASON, using=using):
        row = AppAvailability.all_objects.using(using).filter(app_label=app_label).first()
        if row is None:
            AppAvailability(
                app_label=app_label, mode=AppAvailability.Mode.ALL, upgrade_state=AppAvailability.UPGRADE_DONE
            ).save(using=using)
        elif row.mode != AppAvailability.Mode.ALL:
            row.mode = AppAvailability.Mode.ALL
            row.save(using=using, update_fields={"mode"})


def activate_app(app_label: str, organization_id: UUID, *, using: str = "default") -> None:
    """Create or enable the organization's ``AppInstallation`` with the default configuration.

    Refuses (``ValueError``) while a dependency is not active in the organization: an operator
    could not reach that state either (#321). A library (#792) is made effective by activating
    the test consumer of ``library_consumer()``, which must be registered.
    """
    from django.db import transaction

    from skeleton.system.app_registry import get_app, is_library, required_app_labels, validate_configuration
    from skeleton.system.models import AppInstallation
    from skeleton.tenancy.context import system_context

    if is_library(app_label):
        app_label = consumer_of(app_label)
    with (
        transaction.atomic(using=using),
        system_context(reason=REASON, organization_id=organization_id, using=using),
    ):
        for dependency in required_app_labels(app_label):
            active = (
                AppInstallation.all_objects.using(using)
                .filter(organization_id=organization_id, app_label=dependency, enabled=True)
                .exists()
            )
            if not active:
                raise ValueError(f"{app_label} depends on {dependency}, which is not active in the organization.")
        record = (
            AppInstallation.all_objects.using(using)
            .filter(organization_id=organization_id, app_label=app_label)
            .first()
        )
        if record is None:
            AppInstallation(
                organization_id=organization_id,
                app_label=app_label,
                enabled=True,
                configuration=validate_configuration(get_app(app_label), {}),
            ).save(using=using)
        elif not record.enabled:
            record.enabled = True
            record.save(using=using, update_fields={"enabled"})


@contextmanager
def apps_open_by_default(app_labels: Iterable[str] | None = None, *, using: str = "default") -> Iterator[None]:
    """Open the apps and activate them in every organization created inside the block.

    The state an upgraded deployment reaches for apps that were active by default before
    #22. Tests that prove the closed default must not use it.
    """
    from contextlib import ExitStack

    from django.db.models.signals import post_save

    from skeleton.system.app_registry import all_apps, is_library, library_labels, required_app_labels
    from skeleton.tenancy.models import Organization

    stack = ExitStack()
    if app_labels is None and library_labels() and not is_registered(LIBRARY_CONSUMER):
        # Every app open includes the libraries: they follow a test app that depends on them (#792).
        stack.enter_context(library_consumer(using=using))
    labels = list(app_labels) if app_labels is not None else [manifest.app_label for manifest in all_apps()]
    labels = [consumer_of(label) if is_library(label) else label for label in labels]
    ordered = list(dict.fromkeys(dependency for label in labels for dependency in (*required_app_labels(label), label)))
    for label in ordered:
        open_app(label, using=using)

    def activate(sender: Any, instance: Any, created: bool, raw: bool = False, **kwargs: Any) -> None:
        if not created or raw:
            return
        from django.db import transaction

        organization_id, alias = instance.pk, kwargs.get("using") or using

        def run() -> None:
            # After the creating transaction, in a context of its own (never the creator's).
            for label in ordered:
                activate_app(label, organization_id, using=alias)

        transaction.on_commit(run, using=alias)

    # One receiver per use: a nested block never disconnects the outer one (#321).
    uid = f"skeleton-testing-open-apps-{uuid4()}"
    post_save.connect(activate, sender=Organization, weak=False, dispatch_uid=uid)
    try:
        yield
    finally:
        post_save.disconnect(sender=Organization, dispatch_uid=uid)
        stack.close()


def is_registered(app_label: str) -> bool:
    from skeleton.system.app_registry import get_app

    try:
        get_app(app_label)
    except KeyError:
        return False
    return True


def consumer_of(library: str) -> str:
    """The registered test consumer that makes ``library`` effective; ``ValueError`` outside library_consumer()."""
    from skeleton.system.app_registry import dependent_app_labels

    if LIBRARY_CONSUMER not in dependent_app_labels(library):
        raise ValueError(f"{library} is a first-party library: use it inside library_consumer() (#792).")
    return LIBRARY_CONSUMER


@contextmanager
def library_consumer(*, using: str = "default") -> Iterator[str]:
    """Register (and open) a test app that depends on every installed library, for the block (#792).

    A library is effective in an organization while an effective app depends on it; this app
    plays that dependent in the library's own tests. Activate it with
    ``activate_app(<library>, organization)``; disabling it closes the libraries. It is removed
    from the registry when the block ends, so no other test sees it.
    """
    from skeleton.system.app_registry import AppManifest, get_app, library_labels, register_app, unregister_app

    libraries = library_labels()
    if not libraries:
        raise ValueError("No first-party library is installed.")
    manifest = AppManifest(
        app_label=LIBRARY_CONSUMER,
        label="Consumidor de teste das bibliotecas",
        description="Test app that depends on every installed first-party library.",
        dependencies=libraries,
        # A real documentation root, so the checks of the block see a complete app (E012).
        documentation_root=get_app(libraries[0]).documentation_root,
        default_enabled=False,
    )
    register_app(manifest)
    try:
        open_app(LIBRARY_CONSUMER, using=using)
        yield LIBRARY_CONSUMER
    finally:
        unregister_app(LIBRARY_CONSUMER)


def activation_revision(app_label: str) -> int:
    """Current CAS revision of the app's activation in the current tenant (0 before the first write)."""
    from skeleton.system.applications import application_state

    return int(application_state(app_label)["revision"])


def grant_table_actions(
    organization_id: UUID, *, role_code: str, table_key: str, actions: Iterable[str], using: str = "default"
) -> None:
    """Fixture: give the role ``role_code`` the ``actions`` on ``table_key`` in the organization (#545).

    Synchronizes the table catalog first, so a table of an app installed for the test exists in it.
    Written in the system context of a fixture, never of the service under test.
    """
    from skeleton.access.models import Role, TablePermission
    from skeleton.system.models import TableDefinition
    from skeleton.system.synchronization import sync_table_catalog
    from skeleton.tenancy.context import system_context

    sync_table_catalog(using=using)
    with system_context(reason="fixture:table-actions", organization_id=organization_id, using=using):
        role = Role.objects.using(using).get(code=role_code)
        table = TableDefinition.objects.using(using).get(model_label=table_key)
        for action in actions:
            if not TablePermission.objects.using(using).filter(role=role, table=table, action=action).exists():
                TablePermission(organization_id=organization_id, role=role, table=table, action=action).save(
                    using=using
                )
