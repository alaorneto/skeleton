"""Public allauth provider_class extensions minimize claims before any lookup writes."""
from allauth.socialaccount.providers.github.provider import GitHubProvider
from allauth.socialaccount.providers.openid_connect.provider import OpenIDConnectProvider
from allauth.socialaccount.providers.openid_connect.provider import OpenIDConnectProviderAccount
from allauth.socialaccount.providers.saml.provider import SAMLProvider
from django.core.exceptions import PermissionDenied

from skeleton.accounts.providers import GOVBR_SCOPES, configured_providers


class MinimalClaimsMixin:
    def _binding(self):
        binding = configured_providers().get(self.sub_id)
        if binding is None:
            raise PermissionDenied('Provider is not configured.')
        return binding

    def extract_extra_data(self, response):
        return {'skeleton_binding': self._binding().fingerprint}

    def sociallogin_from_response(self, request, response):
        from allauth.socialaccount.models import SocialAccount
        sociallogin = super().sociallogin_from_response(request, response)
        binding = self._binding()
        existing = SocialAccount.objects.filter(provider=self.sub_id, uid=sociallogin.account.uid).values_list('extra_data', flat=True).first()
        if existing is not None and existing.get('skeleton_binding') != binding.fingerprint:
            raise PermissionDenied('Provider identity binding changed; use a new provider ID.')
        if existing is not None and isinstance(existing.get('skeleton_pending_email'), str):
            # Preserve server-owned initial admission state across allauth lookup writes.
            sociallogin.account.extra_data['skeleton_pending_email'] = existing['skeleton_pending_email']
        for address in sociallogin.email_addresses:
            address.verified = bool(address.verified and binding.trust_verified_email)
        return sociallogin

    def get_scope_from_request(self, request):
        # Login scopes are code-owned; query parameters never expand consent.
        return self.get_default_scope()

    def get_auth_params_from_request(self, request, action):
        # Preserve only reauthentication semantics supplied by the library.
        from allauth.socialaccount.providers.base.constants import AuthAction
        return {"prompt": "login"} if self.id == "openid_connect" and action == AuthAction.REAUTHENTICATE else {}

    def get_scope(self):
        return self.get_default_scope()


class PrivateOpenIDConnectAccount(OpenIDConnectProviderAccount):
    def to_str(self):
        # The official gov.br sub is CPF. Never render technical identity in
        # connection pages, administration, or the default account fallback.
        return self.get_brand()['name']

    def get_avatar_url(self):
        return None

    def get_profile_url(self):
        return None


class MinimalOpenIDConnectProvider(MinimalClaimsMixin, OpenIDConnectProvider):
    package = "allauth.socialaccount.providers.openid_connect"
    account_class = PrivateOpenIDConnectAccount

    @property
    def oauth2_adapter_class(self):
        from skeleton.accounts.oidc import VerifiedOpenIDConnectAdapter
        return VerifiedOpenIDConnectAdapter

    @property
    def supports_token_authentication(self):
        return False

    @property
    def is_govbr(self):
        return self._binding().protocol == 'govbr'

    def get_default_scope(self):
        return list(GOVBR_SCOPES) if self.is_govbr else super().get_default_scope()

    def redirect(self, request, process, next_url=None, data=None, **kwargs):
        from django.utils.crypto import get_random_string
        nonce = get_random_string(32)
        auth_params = dict(kwargs.pop('auth_params', {}) or {})
        auth_params['nonce'] = nonce
        # These kwargs are stored in allauth's one-use state, never supplied
        # by a query parameter. The callback consumes state before token use.
        kwargs['skeleton_nonce'] = nonce
        kwargs['skeleton_provider'] = self.sub_id
        return super().redirect(request, process, next_url=next_url, data=data,
                                auth_params=auth_params, **kwargs)

    def extract_uid(self, data):
        if self.is_govbr:
            from allauth.socialaccount.providers.oauth2.client import OAuth2Error
            claims = data.get('id_token', data)
            subject = claims.get('sub')
            if not isinstance(subject, str) or not subject:
                raise OAuth2Error('Invalid gov.br subject.')
            return subject
        return super().extract_uid(data)

    def extract_common_fields(self, data):
        if self.is_govbr:
            claims = data.get('id_token', data)
            # Do not copy CPF/preferred_username, images, phone, or account
            # confidence levels into the global user or profile.
            return {'email': claims.get('email')}
        return super().extract_common_fields(data)

    def extract_email_addresses(self, data):
        government = self.is_govbr
        claims = data.get('id_token', data) if government else None
        addresses = super().extract_email_addresses({'id_token': claims} if claims is not None else data)
        for address in addresses:
            proof = address.verified
            address.verified = proof is True or (government and isinstance(proof, str) and proof == 'true')
        return addresses


class MinimalSAMLProvider(MinimalClaimsMixin, SAMLProvider):
    package = "allauth.socialaccount.providers.saml"


class MinimalGitHubProvider(MinimalClaimsMixin, GitHubProvider):
    package = "allauth.socialaccount.providers.github"
