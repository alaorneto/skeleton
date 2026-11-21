"""The settings area of an organization (#621): the hub ``/o/<org>/admin/``.

Only presentation: the hub lists, by group, the entries of ``SETTINGS_GROUPS`` this person sees;
each target view keeps its own authorization. Without any entry besides the ones every member sees
(Tables), the hub does not exist for that person (the same neutral 404 as any missing page).
"""

from django.http import Http404
from django.shortcuts import render
from django.utils.translation import gettext

from skeleton.web.navigation import can_open_settings, settings_navigation_for
from skeleton.web.views import tenant_page

__all__ = ["organization_admin", "organization_public_page", "public_page_editor"]


@tenant_page()
def organization_admin(request, organization_id):
    groups = settings_navigation_for(request, organization_id)
    if not can_open_settings(groups):
        raise Http404
    return render(
        request,
        "skeleton/web/settings/hub.html",
        {"title": gettext("Configuração da organização"), "groups": groups, "organization_id": organization_id},
    )


# --------------------------------------------------------------------------- organization pages (#623, #624)


class _OrganizationPageStore:
    """The editor's store over ``skeleton.tenancy.pages`` for one slot of the context organization."""

    def __init__(self, actor, slot):
        self.actor, self.slot = actor, slot

    def load(self):
        from skeleton.tenancy.pages import organization_page

        return organization_page(self.slot)

    def save(self, *, document, published, expected_revision):
        from skeleton.tenancy.pages import save_organization_page

        return save_organization_page(
            actor=self.actor,
            slot=self.slot,
            document=document,
            published=published,
            expected_revision=expected_revision,
        )


def _require_admin(request):
    from skeleton.access.policies import is_organization_admin

    if not is_organization_admin(request.user):
        raise Http404  # Delegated managers and members: the same neutral 404 as a missing page.


def _public_preview(request, state, organization_id):
    from django.urls import reverse

    return render(
        request,
        "skeleton/web/pages/public.html",
        {
            "blocks": state.blocks,
            "preview": True,
            "published": state.published,
            "back_url": reverse("web:organization-page-public", kwargs={"organization_id": organization_id}),
        },
    )


def public_page_editor(request, organization_id):
    from django.urls import reverse

    from skeleton.common.page_blocks import ORGANIZATION_PUBLIC
    from skeleton.tenancy.organization_settings import current_settings
    from skeleton.web.page_editor import PageEditor

    settings_row = current_settings()
    address_off = settings_row is None or not settings_row.site_enabled
    return PageEditor(
        kind=ORGANIZATION_PUBLIC,
        store=_OrganizationPageStore(request.user, "public"),
        url_name="web:organization-page-public",
        url_kwargs={"organization_id": organization_id},
        base_template="skeleton/web/base.html",
        title=gettext("Página pública"),
        eyebrow=gettext("Organização"),
        description=gettext(
            "O que quem não entrou vê no endereço próprio da organização, com o nome, a cor e o logo dela. "
            "Sem página publicada, o endereço leva ao login, como antes. Os blocos são texto simples."
        ),
        notice=gettext("A página só aparece no endereço próprio da organização, que está desligado.")
        if address_off
        else "",
        notice_link=(
            gettext("Configurações gerais"),
            reverse("web:organization-settings", kwargs={"organization_id": organization_id}),
        )
        if address_off
        else None,
        preview=lambda request, state: _public_preview(request, state, organization_id),
        extra_context={"organization_id": organization_id},  # The organization shell (sidebar).
    )


@tenant_page()
def organization_public_page(request, organization_id, operation, index=None):
    """``/o/<org>/admin/pages/public/``: the shared block editor (#619) over the public page."""
    from skeleton.web.page_editor import dispatch

    _require_admin(request)
    return dispatch(request, public_page_editor(request, organization_id), operation, index=index)
