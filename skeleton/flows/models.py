"""Tables of the flow engine (docs/flows.md, "Modelo de dados").

Every table is an audited ``OrganizationRecord``: process state is business data with a
required trail, so none of them is operational. Authorization is derived from the subject
record and from participation, never from an owner or visibility of their own; none is
exposed in the generic gateway. Only the services of ``skeleton.flows`` write them.
"""

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy

from skeleton.common.models import OrganizationRecord

ACTIVE_STATES = ("running", "suspended")


class FlowDefinition(OrganizationRecord):
    """The frozen canonical specification of one version of a flow in one organization.

    Written once, when the first instance of the version starts there; instances always run
    from this row, never from the code that declared it.
    """

    key = models.CharField(max_length=100)
    version = models.PositiveIntegerField()
    app_label = models.CharField(max_length=100)
    label = models.CharField(max_length=200)
    digest = models.CharField(max_length=64)
    specification = models.JSONField()
    immutable_fields = ("key", "version", "app_label", "label", "digest", "specification")

    class Meta(OrganizationRecord.Meta):
        constraints = OrganizationRecord.Meta.constraints + [
            models.UniqueConstraint(
                fields=("organization", "key", "version"),
                name="sk_flows_definition_version",
                violation_error_code="unique",
            ),
        ]

    def __str__(self):
        return f"{self.key} v{self.version}"


class FlowDraft(OrganizationRecord):
    """A flow authored as data in one organization (#414): a draft, then published versions.

    The specification has the JSON shape of ``FlowSpec.to_json`` (without the version, set on
    publication) and may reference only what the code registered: service handlers, conditions,
    assignment rules and exposed tables of the owning app. Publishing writes an immutable
    ``FlowDefinition`` for the next version; instances run from that row, never from the draft.
    """

    key = models.CharField(max_length=100)
    label = models.CharField(max_length=200)
    app_label = models.CharField(max_length=100)
    specification = models.JSONField(default=dict)
    published_version = models.PositiveIntegerField(null=True, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    immutable_fields = ("key",)

    class Meta(OrganizationRecord.Meta):
        constraints = OrganizationRecord.Meta.constraints + [
            models.UniqueConstraint(
                fields=("organization", "key"),
                condition=Q(deleted_at__isnull=True),
                name="sk_flows_draft_key",
                violation_error_code="unique",
            ),
        ]

    def __str__(self):
        return self.key


class FlowInstance(OrganizationRecord):
    """One execution of a flow about one business record of the owning app."""

    class Status(models.TextChoices):
        RUNNING = "running", gettext_lazy("Em andamento")
        SUSPENDED = "suspended", gettext_lazy("Suspensa")
        COMPLETED = "completed", gettext_lazy("Concluída")
        CANCELLED = "cancelled", gettext_lazy("Cancelada")
        FAILED = "failed", gettext_lazy("Com falha")

    definition = models.ForeignKey(FlowDefinition, on_delete=models.PROTECT, related_name="instances")
    flow_key = models.CharField(max_length=100)
    flow_version = models.PositiveIntegerField()
    number = models.CharField(max_length=64, blank=True, default="")
    # Generic reference validated by the service against the code registry; never a GenericForeignKey.
    subject_table = models.CharField(max_length=128)
    subject_sysid = models.UUIDField()
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.RUNNING)
    outcome = models.CharField(max_length=64, blank=True, default="")
    starter = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    data = models.JSONField(default=dict, blank=True)
    sensitive_data = models.JSONField(default=dict, blank=True)
    idempotency_key = models.CharField(max_length=200, null=True, blank=True)  # noqa: DJ001 - NULL means "no key"
    run_count = models.PositiveIntegerField(default=0)
    activity_count = models.PositiveIntegerField(default=0)
    finished_at = models.DateTimeField(null=True, blank=True)
    immutable_fields = (
        "definition_id",
        "flow_key",
        "flow_version",
        "number",
        "subject_table",
        "subject_sysid",
        "starter_id",
        "idempotency_key",
    )

    class Meta(OrganizationRecord.Meta):
        constraints = OrganizationRecord.Meta.constraints + [
            models.UniqueConstraint(
                fields=("organization", "flow_key", "idempotency_key"),
                condition=Q(idempotency_key__isnull=False),
                name="sk_flows_instance_idempotency",
                violation_error_code="unique",
            ),
            models.UniqueConstraint(
                fields=("organization", "flow_key", "subject_sysid"),
                condition=Q(status__in=ACTIVE_STATES, deleted_at__isnull=True),
                name="sk_flows_instance_active",
                violation_error_code="unique",
            ),
            models.CheckConstraint(
                condition=Q(status__in=("running", "suspended", "completed", "cancelled", "failed")),
                name="sk_flows_instance_status",
            ),
        ]
        indexes = [
            models.Index(fields=("organization", "subject_table", "subject_sysid"), name="sk_flows_instance_subject"),
            models.Index(fields=("organization", "status", "created_at"), name="sk_flows_instance_state"),
        ]

    def __str__(self):
        return self.number or f"{self.flow_key} {self.pk}"


class FlowStepRun(OrganizationRecord):
    """One activation of a step: the token that walks the flow.

    ``scope`` is the run of the innermost fork the token belongs to and ``branch`` the fork
    transition that created it; a join counts the branches that arrived in ``state``.
    """

    class Status(models.TextChoices):
        ACTIVE = "active", gettext_lazy("Ativa")
        WAITING = "waiting", gettext_lazy("Aguardando ramos")
        COMPLETED = "completed", gettext_lazy("Concluída")
        CANCELLED = "cancelled", gettext_lazy("Cancelada")
        FAILED = "failed", gettext_lazy("Com falha")

    instance = models.ForeignKey(FlowInstance, on_delete=models.PROTECT, related_name="runs")
    step_key = models.CharField(max_length=64)
    kind = models.CharField(max_length=16)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
    sequence = models.PositiveIntegerField()
    scope = models.ForeignKey("self", on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    branch = models.CharField(max_length=140, blank=True, default="")
    via = models.CharField(max_length=140, blank=True, default="")
    outcome = models.CharField(max_length=64, blank=True, default="")
    state = models.JSONField(default=dict, blank=True)
    due_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    immutable_fields = ("instance_id", "step_key", "kind", "sequence", "scope_id", "branch", "via")

    class Meta(OrganizationRecord.Meta):
        constraints = OrganizationRecord.Meta.constraints + [
            models.UniqueConstraint(
                fields=("instance", "sequence"), name="sk_flows_run_sequence", violation_error_code="unique"
            ),
            models.CheckConstraint(
                condition=Q(status__in=("active", "waiting", "completed", "cancelled", "failed")),
                name="sk_flows_run_status",
            ),
        ]
        indexes = [models.Index(fields=("organization", "instance", "status"), name="sk_flows_run_state")]

    def __str__(self):
        return f"{self.step_key} #{self.sequence} {self.status}"


class FlowTask(OrganizationRecord):
    """The work of one person (or of one queue) in a task or approval step.

    A queue task has no ``assignee`` and one candidate group or role; closing a task clears
    the candidates, so only open tasks keep a group or a role from being removed.
    """

    class Status(models.TextChoices):
        OPEN = "open", gettext_lazy("Aberta")
        COMPLETED = "completed", gettext_lazy("Concluída")
        CANCELLED = "cancelled", gettext_lazy("Cancelada")

    instance = models.ForeignKey(FlowInstance, on_delete=models.PROTECT, related_name="tasks")
    step_run = models.ForeignKey(FlowStepRun, on_delete=models.PROTECT, related_name="tasks")
    step_key = models.CharField(max_length=64)
    kind = models.CharField(max_length=16)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.OPEN)
    slot = models.PositiveIntegerField(default=0)
    assignee = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    candidate_group = models.ForeignKey(
        "sk_access.AccessGroup", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    candidate_role = models.ForeignKey(
        "sk_access.Role", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    delegated_from = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    decision = models.CharField(max_length=16, blank=True, default="")
    action = models.CharField(max_length=140, blank=True, default="")
    comment = models.TextField(blank=True, default="")
    due_at = models.DateTimeField(null=True, blank=True)
    reminded_count = models.PositiveIntegerField(default=0)
    escalated_at = models.DateTimeField(null=True, blank=True)
    completed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    completed_at = models.DateTimeField(null=True, blank=True)
    immutable_fields = ("instance_id", "step_run_id", "step_key", "kind", "slot")

    class Meta(OrganizationRecord.Meta):
        constraints = OrganizationRecord.Meta.constraints + [
            models.CheckConstraint(
                condition=Q(status__in=("open", "completed", "cancelled")), name="sk_flows_task_status"
            ),
            models.CheckConstraint(
                condition=Q(decision__in=("", "approved", "rejected")), name="sk_flows_task_decision"
            ),
            models.CheckConstraint(
                condition=Q(candidate_group__isnull=True) | Q(candidate_role__isnull=True),
                name="sk_flows_task_one_queue",
            ),
        ]
        indexes = [
            models.Index(fields=("organization", "assignee", "status"), name="sk_flows_task_assignee"),
            models.Index(fields=("organization", "status", "due_at"), name="sk_flows_task_state"),
        ]

    def __str__(self):
        return f"{self.step_key} {self.status}"


class FlowSubmission(OrganizationRecord):
    """One submitted form, kept exactly as it was accepted; sensitive values live apart."""

    instance = models.ForeignKey(FlowInstance, on_delete=models.PROTECT, related_name="submissions")
    step_run = models.ForeignKey(FlowStepRun, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    task = models.ForeignKey(FlowTask, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    step_key = models.CharField(max_length=64, blank=True, default="")
    transition_key = models.CharField(max_length=140, blank=True, default="")
    origin = models.CharField(max_length=16)
    values = models.JSONField(default=dict, blank=True)
    sensitive_values = models.JSONField(default=dict, blank=True)
    immutable_fields = (
        "instance_id",
        "step_run_id",
        "task_id",
        "step_key",
        "transition_key",
        "origin",
        "values",
        "sensitive_values",
    )

    class Meta(OrganizationRecord.Meta):
        constraints = OrganizationRecord.Meta.constraints + [
            models.CheckConstraint(
                condition=Q(origin__in=("start", "task", "decision", "signal", "service")),
                name="sk_flows_submission_origin",
            ),
        ]
        indexes = [models.Index(fields=("organization", "instance", "created_at"), name="sk_flows_submission_log")]

    def __str__(self):
        return f"{self.origin} {self.step_key}"


class FlowActivity(OrganizationRecord):
    """The ordered narrative of an instance: who did what and when (identifiers and labels only)."""

    instance = models.ForeignKey(FlowInstance, on_delete=models.PROTECT, related_name="activities")
    sequence = models.PositiveIntegerField()
    kind = models.CharField(max_length=32)
    step_key = models.CharField(max_length=64, blank=True, default="")
    step_run = models.ForeignKey(FlowStepRun, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    task = models.ForeignKey(FlowTask, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    detail = models.JSONField(default=dict, blank=True)
    comment = models.TextField(blank=True, default="")
    immutable_fields = ("instance_id", "sequence", "kind", "step_key", "step_run_id", "task_id", "detail", "comment")

    class Meta(OrganizationRecord.Meta):
        constraints = OrganizationRecord.Meta.constraints + [
            models.UniqueConstraint(
                fields=("instance", "sequence"), name="sk_flows_activity_sequence", violation_error_code="unique"
            ),
        ]

    def __str__(self):
        return f"{self.kind} #{self.sequence}"
