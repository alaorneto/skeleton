"""The confirmation step of an irreversible action (#769).

An action that cannot be undone, or that affects other people, is never one click away. The
button on the screen is a link to the action's own URL; a GET there shows what is about to
happen (the target, the consequence, a red button and "Cancelar"), and the red button POSTs to
the same URL, exactly as before. The POST is unchanged: same view, same CSRF, same revision.

    @platform_page
    @confirmation_step(_describe_restart)
    @require_POST
    @recent_authentication_required
    def instance_restart(request): ...

``describe(request, *args, **kwargs)`` runs under the same decorators as the POST (the
authorization ones go above ``confirmation_step``) and finds the target with the same authorized
lookup as the view, so a target that does not exist or that the person cannot reach answers the
same neutral 404 on the GET as on the POST. It returns a :class:`Confirmation`, or ``None`` when
the action needs no confirmation (the GET then falls through to the view, which refuses it).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import wraps
from typing import Any

from django.shortcuts import render

PLATFORM_TEMPLATE = "skeleton/web/platform/confirm.html"
ORGANIZATION_TEMPLATE = "skeleton/web/confirm.html"


@dataclass(frozen=True)
class Confirmation:
    """What the confirmation page says and sends back."""

    title: Any
    message: Any
    submit_label: Any
    cancel_url: str
    subject: Any = ""
    eyebrow: Any = ""
    # Hidden fields the POST needs (the revision read now, the item of a list…).
    hidden: dict[str, Any] = field(default_factory=dict)
    # A visible reason field, when the action asks for one (voiding an invoice…).
    reason_label: Any = ""
    reason_required: bool = False
    template: str = ORGANIZATION_TEMPLATE
    context: dict[str, Any] = field(default_factory=dict)


def confirmation_page(request, confirmation: Confirmation, *, error: Any = "", status: int = 200):
    return render(
        request,
        confirmation.template,
        {
            "title": confirmation.title,
            "message": confirmation.message,
            "submit_label": confirmation.submit_label,
            "cancel_url": confirmation.cancel_url,
            "subject_label": confirmation.subject,
            "eyebrow": confirmation.eyebrow,
            "hidden": sorted(confirmation.hidden.items()),
            "reason_label": confirmation.reason_label,
            "reason_required": confirmation.reason_required,
            "error": error,
            **confirmation.context,
        },
        status=status,
    )


def confirmation_step(describe):
    """On GET, show the confirmation of the POST of this same URL; the POST goes through unchanged."""

    def decorator(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            if request.method in ("GET", "HEAD"):
                confirmation = describe(request, *args, **kwargs)
                if confirmation is not None:
                    return confirmation_page(request, confirmation)
            return view(request, *args, **kwargs)

        return wrapper

    return decorator
