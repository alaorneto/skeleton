"""Admission decisions live in adapters, not race-prone social signals."""
from copy import deepcopy

from allauth.account.adapter import DefaultAccountAdapter
from allauth.core.exceptions import ImmediateHttpResponse
from allauth.mfa.adapter import DefaultMFAAdapter
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import HttpResponseForbidden
from django.utils.translation import gettext_lazy as _

from skeleton.accounts.mfa import cipher
from skeleton.accounts.providers import binding_for_sociallogin
from skeleton.accounts.services import ensure_profile, normalized_email, password_allowed
from skeleton.tenancy.invitations import accept_invitation, pending_invitation


def _deny():
    raise ImmediateHttpResponse(HttpResponseForbidden('Identity admission denied.'))


def _accept_pending(request, user):
    proof = pending_invitation(request)
    if proof is None:
        return False
    accept_invitation(user=user, organization_id=proof.organization_id, proof=proof, verified_signup=True)
    # setup_user_email runs after save_user; preserve only this request's proof.
    request._skeleton_verified_invitation_email = proof.email
    request.session.pop('skeleton_invitation', None)
    return True


class AccountAdapter(DefaultAccountAdapter):
    def is_open_for_signup(self, request):
        return pending_invitation(request) is not None or getattr(settings, 'SKELETON_PUBLIC_SIGNUP', False)

    def clean_email(self, email):
        return normalized_email(super().clean_email(email))

    def is_email_verified(self, request, email):
        if getattr(request, "_skeleton_verified_invitation_email", None) == normalized_email(email):
            return True
        proof = pending_invitation(request)
        return bool(proof and proof.email == normalized_email(email)) or super().is_email_verified(request, email)

    def save_user(self, request, user, form, commit=True):
        with transaction.atomic():
            user = super().save_user(request, user, form, commit=False)
            user.email = normalized_email(user.email)
            proof = pending_invitation(request)
            if proof is not None and user.email != proof.email:
                raise ValidationError({'email': 'Invitation email cannot be changed.'})
            if commit:
                user.save()
                ensure_profile(user)
                _accept_pending(request, user)
            return user

    def confirm_email(self, request, email_address):
        with transaction.atomic():
            confirmed = super().confirm_email(request, email_address)
            if confirmed:
                from allauth.socialaccount.models import SocialAccount
                from skeleton.accounts.providers import configured_providers
                from skeleton.tenancy.models import Membership, Organization
                from skeleton.tenancy.context import system_context
                user = email_address.user
                if not user.is_active or user.deleted_at:
                    _deny()
                for account in SocialAccount.objects.filter(user=user):
                    binding = configured_providers().get(account.provider)
                    pending = account.extra_data.get('skeleton_pending_email')
                    if not (binding and binding.jit_provisioning and binding.organization_slug
                            and pending == normalized_email(email_address.email)
                            and account.extra_data.get('skeleton_binding') == binding.fingerprint):
                        continue
                    organization, role = SocialAccountAdapter()._resolve_binding(binding)
                    with system_context(reason='sso:validated', organization_id=organization.pk):
                        Organization.objects.select_for_update().get(pk=organization.pk)
                        # A pending initial admission is not authority to undo local offboarding.
                        existed = Membership.objects.with_deleted().filter(user=user).exists()
                        account.extra_data = {'skeleton_binding': binding.fingerprint}
                        account.save(update_fields=['extra_data'])
                        if not existed:
                            provision_social_membership(user, binding)
            return confirmed

    def send_password_reset_mail(self, user, email, context):
        if password_allowed(user):
            return super().send_password_reset_mail(user, email, context)
        return None

    def set_password(self, user, password):
        from skeleton.accounts.services import set_local_password
        set_local_password(user, password)

    def send_mail(self, template_prefix, email, context):
        # Render now; the commit callback holds only materialized email, never lazy tenant ORM.
        message = self.render_mail(template_prefix, email, context)
        from skeleton.common.mail import schedule_mail
        from allauth.core import context as allauth_context
        schedule_mail(message, request=allauth_context.request)


class SocialAccountAdapter(DefaultSocialAccountAdapter):
    def is_email_verified(self, provider, email):
        # Preserve protocol evidence, never fabricate it from operator/domain overrides.
        return False

    def can_authenticate_by_email(self, login, email):
        return False

    def is_open_for_signup(self, request, sociallogin):
        binding = binding_for_sociallogin(sociallogin)
        return (pending_invitation(request) is not None
                or bool(binding.organization_slug and binding.jit_provisioning)
                or bool(not binding.organization_slug and getattr(settings, 'SKELETON_PUBLIC_SIGNUP', False)))

    def pre_social_login(self, request, sociallogin):
        from skeleton.accounts.models import User
        binding = binding_for_sociallogin(sociallogin)
        connecting = sociallogin.state.get('process') == 'connect'
        if connecting:
            from allauth.account.internal.flows.reauthentication import did_recently_authenticate
            import time
            from allauth.account import app_settings as account_settings
            from allauth.account.authentication import get_authentication_records
            records = get_authentication_records(request)
            recent = bool(records and time.time() - records[-1]['at'] < account_settings.REAUTHENTICATION_TIMEOUT)
            if not request.user.is_authenticated or not request.user.is_active or request.user.deleted_at or not recent or not did_recently_authenticate(request):
                _deny()
        if sociallogin.is_existing:
            user = sociallogin.user
            if user.deleted_at or not user.is_active:
                _deny()
            stored = sociallogin.account.extra_data.get('skeleton_binding')
            if stored != binding.fingerprint:
                _deny()
            if connecting and user.pk != request.user.pk:
                _deny()
        elif not connecting:
            email = normalized_email(sociallogin.user.email) if sociallogin.user.email else ''
            if email and User.all_objects.filter(email=email).exists():
                # Existing emails must log in and explicitly connect after reauthentication.
                _deny()
            if not email and (binding.protocol != 'govbr' or not self.is_open_for_signup(request, sociallogin)):
                _deny()
            proof = pending_invitation(request)
            if proof is not None and email and email != proof.email:
                _deny()
        # Trust only assertions already validated by the library and explicitly permitted by config.
        for address in sociallogin.email_addresses:
            address.verified = bool(address.verified and binding.trust_verified_email)
        # Never persist tokens/claims or allow an IdP to promote platform privileges.
        pending_email = sociallogin.account.extra_data.get('skeleton_pending_email') if sociallogin.is_existing else None
        sociallogin.account.extra_data = {'skeleton_binding': binding.fingerprint}
        if isinstance(pending_email, str):
            sociallogin.account.extra_data['skeleton_pending_email'] = pending_email
        sociallogin.user.is_staff = False if not sociallogin.is_existing else sociallogin.user.is_staff
        sociallogin.user.is_superuser = False if not sociallogin.is_existing else sociallogin.user.is_superuser
        # Protocol validation/token use has completed before this hook. Pending signup
        # must not retain credentials or arbitrary protocol state in django_session.
        sociallogin.token = None
        sociallogin.state = {key: sociallogin.state[key] for key in ('process', 'next', 'headless')
                             if key in sociallogin.state}
        if binding.organization_slug:
            self._resolve_binding(binding)
        if connecting and binding.protocol == 'govbr':
            # Connecting also opens a government browser session. Only a
            # validated, recently authenticated flow can set this provider ID.
            request.session['skeleton_govbr_logout_provider'] = binding.provider_id

    def _resolve_binding(self, binding):
        from skeleton.tenancy.models import Organization
        from skeleton.tenancy.context import system_context
        from skeleton.access.models import Role
        organization = Organization.objects.filter(slug=binding.organization_slug, is_active=True).first()
        if organization is None:
            _deny()
        with system_context(reason='sso:validated', organization_id=organization.pk):
            role = Role.objects.filter(code=binding.default_role_code).first()
            if role is None:
                _deny()
            return organization, role

    def save_user(self, request, sociallogin, form=None):
        binding = binding_for_sociallogin(sociallogin)
        with transaction.atomic():
            if not self.is_open_for_signup(request, sociallogin):
                _deny()
            proof = pending_invitation(request)
            email = form.cleaned_data.get('email') if form is not None else sociallogin.user.email
            email = normalized_email(email or '')
            from skeleton.accounts.models import User
            if not email or User.all_objects.filter(email=email).exists():
                _deny()
            if proof is not None and email != proof.email:
                _deny()
            sociallogin.user.local_password_allowed = False
            sociallogin.user.set_unusable_password()
            user = super().save_user(request, sociallogin, form=form)
            ensure_profile(user)
            invited = _accept_pending(request, user)
            if not invited and binding.jit_provisioning and binding.organization_slug:
                # Never provision a membership from an unverified email; allauth confirmation can
                # later finish this same binding via email_confirmed without trusting external claims.
                from allauth.account.models import EmailAddress
                if EmailAddress.objects.filter(user=user, email__iexact=user.email, verified=True).exists():
                    provision_social_membership(user, binding)
                else:
                    sociallogin.account.extra_data = {
                        'skeleton_binding': binding.fingerprint,
                        'skeleton_pending_email': normalized_email(user.email),
                    }
                    sociallogin.account.save(update_fields=['extra_data'])
            return user


def provision_social_membership(user, binding):
    from skeleton.access.services import provision_membership
    from skeleton.tenancy.context import system_context
    organization, role = SocialAccountAdapter()._resolve_binding(binding)
    with system_context(reason='sso:validated', organization_id=organization.pk):
        return provision_membership(organization=organization, user=user, role=role)


class EncryptedMFAAdapter(DefaultMFAAdapter):
    def encrypt(self, text):
        return cipher().encrypt(text.encode()).decode()

    def decrypt(self, encrypted_text):
        return cipher().decrypt(encrypted_text.encode()).decode()
