from django.db import models
from skeleton.common.models import OrganizationRecord, SystemRecord


class TableDefinition(SystemRecord):
    database_alias = models.CharField(max_length=64)
    schema_name = models.CharField(max_length=64)
    db_table = models.CharField(max_length=128)
    model_label = models.CharField(max_length=128, null=True, blank=True)
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
        constraints = SystemRecord.Meta.constraints + [models.UniqueConstraint(fields=("database_alias", "schema_name", "db_table"), name="sk_system_table_physical_unique")]

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
    relation_target = models.CharField(max_length=128, null=True, blank=True)
    sensitive = models.BooleanField(default=False)
    available = models.BooleanField(default=True)

    class Meta(SystemRecord.Meta):
        constraints = SystemRecord.Meta.constraints + [models.UniqueConstraint(fields=("table", "column"), name="sk_system_field_column_unique")]


class AppInstallation(OrganizationRecord):
    app_label = models.CharField(max_length=100)
    enabled = models.BooleanField(default=True)
    configuration = models.JSONField(default=dict, blank=True)
    immutable_fields = ("app_label",)

    class Meta(OrganizationRecord.Meta):
        constraints = OrganizationRecord.Meta.constraints + [
            models.UniqueConstraint(fields=("organization", "app_label"), name="sk_system_appinstallation_unique"),
        ]

    def clean(self):
        super().clean()
        from .app_registry import get_app, validate_configuration
        from django.core.exceptions import ValidationError
        try:
            manifest = get_app(self.app_label)
        except KeyError:
            loaded = getattr(self, "_loaded_values", {})
            retained = ComponentInstallation.objects.filter(app_label=self.app_label, state="schema_retained").exists()
            if (retained and not self._state.adding and not self.enabled
                    and self.configuration == loaded.get("configuration")):
                # Retention can disable existing state, never edit or reactivate it.
                return
            raise ValidationError({"app_label": "Application is not installed."}) from None
        self.configuration = validate_configuration(manifest, self.configuration)


class ComponentInstallation(SystemRecord):
    """Global deployment state; unrelated to an organization's activation."""
    component_id = models.CharField(max_length=100, unique=True)
    kind = models.CharField(max_length=8, choices=(("core", "Core"), ("app", "App")))
    app_label = models.CharField(max_length=100, blank=True)
    package_name = models.CharField(max_length=200, blank=True)
    version = models.CharField(max_length=64)
    digest = models.CharField(max_length=64, blank=True)
    publisher = models.CharField(max_length=100, blank=True)
    generation = models.PositiveBigIntegerField(default=0)
    state = models.CharField(max_length=24, choices=(
        ("installed", "Instalado"), ("project-local", "Local"),
        ("schema_retained", "Dados preservados"),
    ))
    inventory_reference = models.CharField(max_length=200, blank=True)
    immutable_fields = ("component_id", "kind", "app_label", "package_name")

    def __str__(self):
        return f"{self.component_id} {self.version}"


class DeploymentOperation(SystemRecord):
    """Approval and journal mirror; result retains submission identity until ACK."""
    operation = models.CharField(max_length=16, choices=(
        ("install", "Instalação"), ("update", "Atualização"),
        ("uninstall", "Desinstalação"),
        ("reinstall", "Reinstalação"), ("core_update", "Atualização do núcleo"),
    ))
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
