"""LDAP as an allauth provider (#32), so it reuses ``pre_social_login``, MFA and JIT.

Each database ``IdentityProvider`` with protocol ``ldap`` becomes an app of this provider
(``provider_id`` is its sub id). There is no redirect: the login view binds against the
directory and hands a ``SocialLogin`` to ``complete_social_login``. The identity is the
directory's ``entryUUID``/``objectGUID``, never the email, so a new email in the directory
keeps the same identity and a matching local email never connects by itself.
"""

from __future__ import annotations

from typing import Any

from allauth.account.models import EmailAddress
from allauth.socialaccount.providers.base import Provider, ProviderAccount
from django.core.exceptions import PermissionDenied
from django.urls import reverse


class LDAPAccount(ProviderAccount):
    def to_str(self) -> str:
        return self.account.get_provider().name


class LDAPProvider(Provider):
    id = "ldap"
    name = "LDAP"
    package = "skeleton.accounts.ldap"
    account_class = LDAPAccount
    uses_apps = True
    supports_redirect = False

    def __init__(self, request: Any, app: Any = None) -> None:
        super().__init__(request, app=app)
        if self.app is not None and self.app.name:
            self.name = self.app.name

    def _binding(self) -> Any:
        from skeleton.accounts.providers import configured_providers

        binding = configured_providers().get(self.sub_id)
        if binding is None:
            raise PermissionDenied("Provider is not configured.")
        return binding

    def get_login_url(self, request: Any, **kwargs: Any) -> str:
        return reverse("ldap_login", kwargs={"provider_id": self.sub_id})

    def extract_uid(self, data: dict[str, Any]) -> str:
        return str(data["uid"])

    def extract_extra_data(self, data: dict[str, Any]) -> dict[str, Any]:
        # Only the binding, never directory attributes or the DN.
        return {"skeleton_binding": self._binding().fingerprint}

    def extract_common_fields(self, data: dict[str, Any]) -> dict[str, Any]:
        return {
            "email": data.get("email") or "",
            "first_name": data.get("first_name") or "",
            "last_name": data.get("last_name") or "",
        }

    def extract_email_addresses(self, data: dict[str, Any]) -> list[EmailAddress]:
        email = data.get("email")
        if not email:
            return []
        # The directory attests the address only when the platform said it may.
        return [EmailAddress(email=email, verified=bool(self._binding().trust_verified_email), primary=True)]

    def sociallogin_from_response(self, request: Any, response: dict[str, Any]) -> Any:
        from allauth.socialaccount.models import SocialAccount

        sociallogin = super().sociallogin_from_response(request, response)
        binding = self._binding()
        existing = (
            SocialAccount.objects.filter(provider=self.sub_id, uid=sociallogin.account.uid)
            .values_list("extra_data", flat=True)
            .first()
        )
        if existing is not None and existing.get("skeleton_binding") != binding.fingerprint:
            raise PermissionDenied("Provider identity binding changed; use a new provider ID.")
        if existing is not None and isinstance(existing.get("skeleton_pending_email"), str):
            sociallogin.account.extra_data["skeleton_pending_email"] = existing["skeleton_pending_email"]
        return sociallogin


provider_classes = [LDAPProvider]
