"""Job queue at /platform/jobs/ (#40): explicit views over ``skeleton.jobs.operations``.

The list shows type, queue, organization, states and error codes, never the payload. A dead
job can be resent (attempts back to zero) or discarded; both need a recent authentication,
POST with CSRF and the ``updated_at`` the operator saw (CAS). GET never changes state.
"""

from __future__ import annotations

from django import forms
from django.apps import apps
from django.contrib import messages
from django.shortcuts import redirect, render
from django.utils.translation import gettext, gettext_lazy
from django.views.decorators.http import require_http_methods, require_safe

from skeleton.accounts.reauthentication import recent_authentication_required
from skeleton.common.exceptions import ConcurrentUpdate
from skeleton.jobs.models import Job
from skeleton.jobs.operations import discard_dead_job, jobs_for_operators, queue_health, resend_dead_job, state_counts
from skeleton.web.views import pagination

from .guards import platform_page


class JobFilterForm(forms.Form):
    state = forms.ChoiceField(required=False, choices=(("", gettext_lazy("Todos")), *Job.State.choices))
    queue = forms.RegexField(required=False, max_length=64, regex=r"^[A-Za-z0-9_.:-]*$")
    job_type = forms.RegexField(required=False, max_length=100, regex=r"^[A-Za-z0-9_.:-]*$")


class JobActionForm(forms.Form):
    action = forms.ChoiceField(choices=(("resend", gettext_lazy("Reenviar")), ("discard", gettext_lazy("Descartar"))))
    expected_updated_at = forms.DateTimeField()


def _organization_names(rows):
    ids = {row.job_organization_id for row in rows if row.job_organization_id}
    if not ids:
        return {}
    Organization = apps.get_model("sk_tenancy", "Organization")
    return dict(Organization.objects.filter(pk__in=ids).values_list("pk", "name"))


def _page(request):
    form = JobFilterForm(request.GET or None)
    filters = form.cleaned_data if form.is_valid() else {}
    filters = {key: value for key, value in filters.items() if value}
    rows = pagination(request, jobs_for_operators(**filters), validated_filters=filters)
    names = _organization_names(rows)
    for row in rows:
        row.organization_name = names.get(row.job_organization_id, "")
    return render(
        request,
        "skeleton/web/platform/jobs.html",
        {
            "platform_section": "jobs",
            "rows": rows,
            "filter_form": form,
            "counts": state_counts(),
            "health": queue_health(),
            "dead_state": Job.State.DEAD,
        },
    )


@platform_page
@require_safe
def jobs(request):
    return _page(request)


@platform_page
@require_http_methods(["POST"])
@recent_authentication_required
def job_action(request, sysid):
    form = JobActionForm(request.POST)
    if not form.is_valid():
        messages.error(request, gettext("Pedido inválido. Atualize a página e tente de novo."))
        return redirect("platform:jobs")
    data = form.cleaned_data
    change = resend_dead_job if data["action"] == "resend" else discard_dead_job
    try:
        change(sysid, expected_updated_at=data["expected_updated_at"], actor=request.user)
    except ConcurrentUpdate:
        messages.error(request, gettext("O job mudou ou já não está esgotado. Atualize a página."))
        return redirect("platform:jobs")
    messages.success(
        request, gettext("Job reenviado para a fila.") if data["action"] == "resend" else gettext("Job descartado.")
    )
    return redirect("platform:jobs")
