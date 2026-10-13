"""Always validate the ID-token signature, including back-channel code exchange."""
from allauth.account.internal.decorators import login_not_required
from allauth.socialaccount.models import SocialApp
from allauth.socialaccount.providers.openid_connect.views import OpenIDConnectOAuth2Adapter
from allauth.socialaccount.providers.oauth2.views import OAuth2CallbackView, OAuth2LoginView
from allauth.socialaccount.providers.oauth2.client import OAuth2Error
from django.http import Http404
from django.utils.crypto import constant_time_compare

from skeleton.accounts.providers import govbr_openid_configuration


class VerifiedOpenIDConnectAdapter(OpenIDConnectOAuth2Adapter):
    expected_nonce = None

    @property
    def openid_config(self):
        from skeleton.accounts.providers import configured_providers
        binding = configured_providers().get(self.provider_id)
        if binding is not None and binding.protocol == 'govbr':
            return govbr_openid_configuration(binding.app['settings']['govbr_environment'])
        return super().openid_config

    def _decode_id_token(self, app, id_token):
        try:
            data = super()._decode_id_token(app, id_token)
        except (KeyError, TypeError, ValueError) as exc:
            raise OAuth2Error('Invalid OIDC token structure.') from exc
        if (not isinstance(data.get('sub'), str) or not data['sub']
                or any(type(data.get(key)) not in (int, float) for key in ('iat', 'exp'))):
            raise OAuth2Error('Missing required OIDC identity or time claims.')
        nonce = data.get('nonce')
        if not (isinstance(nonce, str) and isinstance(self.expected_nonce, str)
                and constant_time_compare(nonce, self.expected_nonce)):
            raise OAuth2Error('Invalid OIDC nonce.')
        return data

    def complete_login(self, request, app, token, **kwargs):
        if not kwargs.get('response', {}).get('id_token'):
            raise OAuth2Error('An OIDC ID token is required.')
        fetched = self.did_fetch_access_token
        # allauth65.19.7 legitimately skips verification for a TLS back-channel token.
        # This skeleton deliberately requires stronger, consistent signature verification.
        self.did_fetch_access_token = False
        try:
            return super().complete_login(request, app, token, **kwargs)
        finally:
            self.did_fetch_access_token = fetched


class VerifiedOpenIDConnectCallbackView(OAuth2CallbackView):
    def _get_state(self, request, provider):
        state, response = super()._get_state(request, provider)
        if response is None:
            if state.get('skeleton_provider') != provider.sub_id or not isinstance(state.get('skeleton_nonce'), str):
                from allauth.socialaccount.helpers import render_authentication_error
                return None, render_authentication_error(request, provider)
            self.adapter.expected_nonce = state['skeleton_nonce']
        return state, response


@login_not_required
def login(request, provider_id):
    try:
        return OAuth2LoginView.adapter_view(VerifiedOpenIDConnectAdapter(request, provider_id))(request)
    except SocialApp.DoesNotExist as exc:
        raise Http404 from exc


@login_not_required
def callback(request, provider_id):
    try:
        response = VerifiedOpenIDConnectCallbackView.adapter_view(VerifiedOpenIDConnectAdapter(request, provider_id))(request)
        from skeleton.accounts.providers import configured_providers
        binding = configured_providers().get(provider_id)
        if binding is not None and binding.protocol == 'govbr' and not 300 <= response.status_code < 400:
            from django.shortcuts import redirect
            # Even denied admission/errors must leave the code-bearing URL.
            # No credentials or protocol payload are copied to the error page.
            return redirect('socialaccount_login_error')
        return response
    except SocialApp.DoesNotExist as exc:
        raise Http404 from exc
