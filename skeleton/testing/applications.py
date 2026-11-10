"""Open and activate apps in tests, now that installed apps are closed by default (#22).

Availability and activation default closed: a fresh test database has no app that any
organization can use. Tests of an app's behavior first state the precondition an
operator would create, with these helpers, instead of relying on the manifest.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from typing import Any
from uuid import UUID

REASON = "test:app-activation"


def open_app(app_label: str, *, using: str = "default") -> None:
    """Make an installed app available to every organization (``mode=all``)."""
    from django.db import transaction

    from skeleton.system.models import AppAvailability
    from skeleton.tenancy.context import system_context

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
    """Create or enable the organization's ``AppInstallation`` with the default configuration."""
    from django.db import transaction

    from skeleton.system.app_registry import get_app, validate_configuration
    from skeleton.system.models import AppInstallation
    from skeleton.tenancy.context import system_context

    with (
        transaction.atomic(using=using),
        system_context(reason=REASON, organization_id=organization_id, using=using),
    ):
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
    from django.db.models.signals import post_save

    from skeleton.system.app_registry import all_apps, required_labels
    from skeleton.tenancy.models import Organization

    labels = list(app_labels) if app_labels is not None else [manifest.app_label for manifest in all_apps()]
    ordered = list(dict.fromkeys(dependency for label in labels for dependency in (*required_labels(label), label)))
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

    post_save.connect(activate, sender=Organization, weak=False, dispatch_uid="skeleton-testing-open-apps")
    try:
        yield
    finally:
        post_save.disconnect(sender=Organization, dispatch_uid="skeleton-testing-open-apps")


def activation_revision(app_label: str) -> int:
    """Current CAS revision of the app's activation in the current tenant (0 before the first write)."""
    from skeleton.system.applications import application_state

    return int(application_state(app_label)["revision"])
