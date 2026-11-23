import uuid

from django.db import models
from django.utils.translation import gettext, gettext_lazy

from skeleton.common.models import OrganizationRecord, SystemRecord


class TableDefinition(SystemRecord):
    database_alias = models.CharField(max_length=64)
    schema_name = models.CharField(max_length=64)
    db_table = models.CharField(max_length=128)
    model_label = models.CharField(max_length=128, null=True, blank=True)  # noqa: DJ001 - semântica NULL já persistida; mudar exige migration
    label = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    origin = models.CharField(max_length=24)
    scope = models.CharField(max_length=24)
    base_class = models.CharField(max_length=128, blank=True)
    uuid_managed = models.BooleanField(default=False)
    soft_delete = models.BooleanField(default=False)
    audit_mode = models.CharField(max_length=32, default="none")
    api_exposed = models.BooleanField(default=False)
    available = models.BooleanField(default=True)

    class Meta(SystemRecord.Meta):
        constraints = SystemRecord.Meta.constraints + [
            models.UniqueConstraint(
                fields=("database_alias", "schema_name", "db_table"), name="sk_system_table_physical_unique"
            )
        ]

    def __str__(self):
        return self.label


class FieldDefinition(SystemRecord):
    table = models.ForeignKey(TableDefinition, on_delete=models.PROTECT, related_name="fields")
    name = models.CharField(max_length=128)
    column = models.CharField(max_length=128)
    label = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    type_name = models.CharField(max_length=128)
    nullable = models.BooleanField(default=False)
    primary_key = models.BooleanField(default=False)
    relation_target = models.CharField(max_length=128, null=True, blank=True)  # noqa: DJ001 - semântica NULL já persistida; mudar exige migration
    sensitive = models.BooleanField(default=False)
    available = models.BooleanField(default=True)

    class Meta(SystemRecord.Meta):
        constraints = SystemRecord.Meta.constraints + [
            models.UniqueConstraint(fields=("table", "column"), name="sk_system_field_column_unique")
        ]


class AppInstallation(OrganizationRecord):
    app_label = models.CharField(max_length=100)
    enabled = models.BooleanField(default=False)  # Closed by default (#283).
    configuration = models.JSONField(default=dict, blank=True)
    immutable_fields = ("app_label",)

    class Meta(OrganizationRecord.Meta):
        constraints = OrganizationRecord.Meta.constraints + [
            models.UniqueConstraint(fields=("organization", "app_label"), name="sk_system_appinstallation_unique"),
        ]

    def clean(self):
        super().clean()
        from django.core.exceptions import ValidationError

        from .app_registry import get_app, validate_configuration

        try:
            manifest = get_app(self.app_label)
        except KeyError:
            loaded = getattr(self, "_loaded_values", {})
            retained = ComponentInstallation.objects.filter(app_label=self.app_label, state="schema_retained").exists()
            if (
                retained
                and not self._state.adding
                and not self.enabled
                and self.configuration == loaded.get("configuration")
            ):
                # Retention can disable existing state, never edit or reactivate it.
                return
            raise ValidationError({"app_label": gettext("Aplicativo não instalado.")}) from None
        self.configuration = validate_configuration(manifest, self.configuration)


class ComponentInstallation(SystemRecord):
    """Global deployment state; unrelated to an organization's activation."""

    component_id = models.CharField(max_length=100, unique=True)
    kind = models.CharField(max_length=8, choices=(("core", "Core"), ("app", "App"), ("bundled", "First-party")))
    app_label = models.CharField(max_length=100, blank=True)
    package_name = models.CharField(max_length=200, blank=True)
    version = models.CharField(max_length=64)
    digest = models.CharField(max_length=64, blank=True)
    publisher = models.CharField(max_length=100, blank=True)
    generation = models.PositiveBigIntegerField(default=0)
    state = models.CharField(
        max_length=24,
        choices=(
            ("installed", gettext_lazy("Instalado")),
            ("project-local", gettext_lazy("Local")),
            ("bundled", "First-party"),
            ("schema_retained", gettext_lazy("Dados preservados")),
        ),
    )
    inventory_reference = models.CharField(max_length=200, blank=True)
    immutable_fields = ("component_id", "kind", "app_label", "package_name")

    def __str__(self):
        return f"{self.component_id} {self.version}"


class DeploymentOperation(SystemRecord):
    """Approval and journal mirror; result retains submission identity until ACK."""

    operation = models.CharField(
        max_length=16,
        choices=(
            ("install", gettext_lazy("Instalação")),
            ("update", gettext_lazy("Atualização")),
            ("uninstall", gettext_lazy("Desinstalação")),
            ("reinstall", gettext_lazy("Reinstalação")),
            ("core_update", gettext_lazy("Atualização do núcleo")),
        ),
    )
    state = models.CharField(max_length=24, default="preview")
    stage = models.CharField(max_length=64, default="preview")
    expected_generation = models.PositiveBigIntegerField()
    plan_digest = models.CharField(max_length=64)
    plan = models.JSONField(default=dict)
    result = models.JSONField(default=dict, blank=True)
    error_code = models.CharField(max_length=100, blank=True)
    immutable_fields = ("operation", "expected_generation", "plan_digest", "plan")

    def __str__(self):
        return f"{self.operation}: {self.state}"


class AppAvailability(SystemRecord):
    """Platform gate: whether an installed app may be used by every organization or only by grants."""

    class Mode(models.TextChoices):
        ALL = "all", gettext_lazy("Todas as organizações")
        SELECTED = "selected", gettext_lazy("Organizações selecionadas")

    app_label = models.CharField(max_length=100, unique=True)
    mode = models.CharField(max_length=16, choices=Mode.choices, default=Mode.SELECTED)
    # "" marks a row written before #22 that upgrade_app_activation has not processed yet;
    # "pending" is mid-upgrade and "done" is final. New code always writes "done".
    upgrade_state = models.CharField(max_length=24, blank=True, default="")
    # What the app adds to each organization limit where it is effective (#696): {"members": 2}.
    # A commercial decision of this instance, never of the manifest; written by set_app_limits.
    limits = models.JSONField(default=dict, blank=True)
    immutable_fields = ("app_label",)
    UPGRADE_PENDING = "pending"
    UPGRADE_DONE = "done"

    class Meta(SystemRecord.Meta):
        constraints = SystemRecord.Meta.constraints + [
            models.CheckConstraint(
                condition=models.Q(mode__in=("all", "selected")), name="sk_system_appavailability_mode"
            ),
        ]

    def __str__(self):
        return f"{self.app_label}: {self.mode}"


class AppOrganizationGrant(SystemRecord):
    """One organization admitted to a ``selected`` app; revoking is a soft delete restored on re-grant."""

    availability = models.ForeignKey(AppAvailability, on_delete=models.PROTECT, related_name="grants")
    # Not named ``organization``: that name turns on tenant semantics for SystemRecord models (E017).
    granted_organization = models.ForeignKey(
        "sk_tenancy.Organization", on_delete=models.PROTECT, related_name="app_grants"
    )
    immutable_fields = ("availability_id", "granted_organization_id")

    class Meta(SystemRecord.Meta):
        constraints = SystemRecord.Meta.constraints + [
            models.UniqueConstraint(
                fields=("availability", "granted_organization"),
                name="sk_system_apporganizationgrant_unique",
                violation_error_code="unique",
            ),
        ]


class OrganizationLimitOverride(SystemRecord):
    """The platform's adjustment of one organization limit (#697): a bonus or a fixed limit.

    A bonus adds ``value`` to the calculated limit; a fixed limit replaces it (``value`` NULL:
    unlimited). One row per organization and metric: removing is a soft delete and setting it
    again restores the same row. An expired row is ignored without any job.
    """

    class Mode(models.TextChoices):
        BONUS = "bonus", gettext_lazy("Bônus")
        FIXED = "fixed", gettext_lazy("Limite fixo")

    # Not named ``organization``: that name turns on tenant semantics for SystemRecord models (E017).
    limited_organization = models.ForeignKey(
        "sk_tenancy.Organization", on_delete=models.PROTECT, related_name="limit_overrides"
    )
    metric = models.CharField(max_length=100)
    mode = models.CharField(max_length=8, choices=Mode.choices)
    value = models.PositiveIntegerField(null=True, blank=True)  # NULL only for FIXED: unlimited.
    reason = models.CharField(max_length=500)
    expires_at = models.DateTimeField(null=True, blank=True)
    immutable_fields = ("limited_organization_id", "metric")

    class Meta(SystemRecord.Meta):
        constraints = SystemRecord.Meta.constraints + [
            models.UniqueConstraint(
                fields=("limited_organization", "metric"),
                name="sk_system_organizationlimitoverride_unique",
                violation_error_code="unique",
            ),
            models.CheckConstraint(
                condition=models.Q(mode__in=("bonus", "fixed")), name="sk_system_organizationlimitoverride_mode"
            ),
            models.CheckConstraint(
                condition=models.Q(mode="fixed") | models.Q(value__gte=1),
                name="sk_system_organizationlimitoverride_bonus_value",
            ),
        ]

    def __str__(self):
        return f"{self.limited_organization_id} {self.metric}: {self.mode} {self.value}"


class InstanceIdentity(SystemRecord):
    """The identity of this database (#708): one row, created by migration, never deleted.

    ``instance_id`` is generated once and never changes; the platform API (#711) and the
    connections of a central instance (#712) recognize the instance by it.
    """

    instance_id = models.UUIDField(unique=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200, blank=True)
    purpose = models.CharField(max_length=500, blank=True)
    # Always true and unique: a second row is refused by the database.
    singleton = models.BooleanField(default=True, unique=True, editable=False)
    immutable_fields = ("instance_id", "singleton")

    class Meta(SystemRecord.Meta):
        constraints = SystemRecord.Meta.constraints + [
            models.CheckConstraint(condition=models.Q(singleton=True), name="sk_system_instanceidentity_singleton"),
        ]

    def __str__(self):
        return str(self.instance_id)


# Native client distribution (#606): defined next to their services, registered in this app.
from .native_clients.models import NativeClientChannel, NativeClientRelease, NativeClientSync  # noqa: E402, F401
