from allauth.account.views import LogoutView
from allauth.account.internal.decorators import login_not_required
from django.utils.decorators import method_decorator
from django.utils.http import urlencode
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from skeleton.tenancy.invitations import InvalidInvitation, accept_invitation, validate_invitation


@require_http_methods(['GET', 'POST'])
def invitation_view(request, organization_id, token):
    try:
        proof = validate_invitation(organization_id, token)
    except (InvalidInvitation, ValidationError):
        return render(request, 'account/invitation_invalid.html', status=400)
    request.session['skeleton_invitation'] = {'organization': str(organization_id), 'invitation': str(proof.sysid), 'digest': proof.token_digest}
    if request.method == 'POST':
        if not request.user.is_authenticated:
            return redirect('account_signup')
        from allauth.account.internal.flows.reauthentication import did_recently_authenticate
        if not did_recently_authenticate(request):
            from django.urls import reverse
            return redirect(reverse('account_reauthenticate') + '?next=' + request.path)
        try:
            accept_invitation(user=request.user, organization_id=organization_id, token=token)
        except ValidationError:
            return render(request, 'account/invitation_invalid.html', status=400)
        request.session.pop('skeleton_invitation', None)
        return redirect('web:dashboard', organization_id=organization_id)
    return render(request, 'account/invitation.html', {'invitation_email': proof.email})


@method_decorator(login_not_required, name='dispatch')
class ProviderLogoutView(LogoutView):
    """End the local allauth session before a reviewed browser logout redirect."""
    def get_redirect_url(self):
        from allauth.account.internal.flows.login import AUTHENTICATION_METHODS_SESSION_KEY
        from skeleton.accounts.providers import configured_providers, govbr_openid_configuration
        if self.request.method == 'POST' and self.request.user.is_authenticated:
            methods = self.request.session.get(AUTHENTICATION_METHODS_SESSION_KEY, [])
            providers = [self.request.session.get('skeleton_govbr_logout_provider')]
            providers.extend(method.get('provider') for method in reversed(methods)
                             if method.get('method') == 'socialaccount')
            for provider_id in providers:
                binding = configured_providers().get(provider_id)
                if binding is not None and binding.protocol == 'govbr':
                    options = binding.app['settings']
                    endpoint = govbr_openid_configuration(options['govbr_environment'])['end_session_endpoint']
                    return endpoint + '?' + urlencode({
                        'post_logout_redirect_uri': options['govbr_logout_redirect_uri'],
                    })
        return super().get_redirect_url()


logout = ProviderLogoutView.as_view()
