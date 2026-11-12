"""``/profile/privacy/``: the person downloads their own data (#190).

The page explains what the export contains; the download is an aggregate route (no single
tenant transaction) that builds the ZIP for the authenticated person only and returns it as an
attachment, never cached. A recent authentication is required, as for other account actions.
"""

from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe

from skeleton.accounts.reauthentication import recent_authentication_required
from skeleton.tenancy.aggregate import aggregate_route


@login_required
@never_cache
@require_safe
def privacy(request):
    return render(request, "skeleton/web/privacy.html", {})


@aggregate_route
@login_required
@never_cache
@require_safe
@recent_authentication_required
def privacy_export(request):
    from skeleton.accounts.privacy import export_my_data
    from skeleton.common.logging import security_event
    from skeleton.common.responses import protect

    content = export_my_data(request.user)
    security_event("privacy.export", target_sysid=request.user.pk, outcome="success")
    response = HttpResponse(content, content_type="application/zip")
    return protect(response, filename=f"meus-dados-{timezone.localdate().isoformat()}.zip")
