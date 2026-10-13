from django import forms
from django.http import Http404
from django.shortcuts import redirect
from django.views.decorators.http import require_POST
from skeleton.common.exceptions import ConcurrentUpdate
from skeleton.tenancy.invitations import resend_invitation
from skeleton.tenancy.models import Invitation
from skeleton.web.views import tenant_page


class ResendForm(forms.Form):
    revision = forms.IntegerField(min_value=1)


@tenant_page(admin=True)
@require_POST
def resend(request, organization_id, sysid):
    invitation = Invitation.objects.filter(pk=sysid).first()
    if invitation is None:
        raise Http404
    form = ResendForm(request.POST)
    if set(request.POST) - {'revision', 'csrfmiddlewaretoken'} or not form.is_valid():
        from django.core.exceptions import ValidationError
        raise ValidationError('Revisão inválida.')
    try:
        resend_invitation(invitation, user=request.user, revision=form.cleaned_data['revision'], request=request)
    except ConcurrentUpdate:
        from django.shortcuts import render
        return render(request, 'skeleton/web/error.html', {'detail': 'Convite alterado. Atualize a lista.'}, status=409)
    return redirect('web:access-list', organization_id=organization_id, resource='invitations')
