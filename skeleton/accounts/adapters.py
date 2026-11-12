"""Admission decisions live in adapters, not race-prone social signals."""

from allauth.account.adapter import DefaultAccountAdapter
from allauth.core.exceptions import ImmediateHttpResponse
from allauth.mfa.adapter import DefaultMFAAdapter
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import HttpResponseForbidden

from skeleton.accounts.mfa import cipher
from skeleton.accounts.providers import admission_for, binding_for_sociallogin
from skeleton.accounts.services import ensure_profile, normalized_email, password_allowed
from skeleton.tenancy.invitations import accept_invitation, pending_invitation


def _domain_admission(user, email):
    """Admission by verified email domain (#28); skipped if this request already chose a tenant."""
    from skeleton.tenancy.context import InvalidContext
    from skeleton.tenancy.domains import apply_verified_email_admission

    try:
        return apply_verified_email_admission(user, email)
    except InvalidContext:
        # Another admission (link, invitation, SSO) already used this request's tenant; the
        # person can still use the domain later from the home page suggestion.
        return "skipped"


def _deny():
    raise ImmediateHttpResponse(HttpResponseForbidden("Identity admission denied."))


def _accept_pending(request, user):
    proof = pending_invitation(request)
    if proof is None:
        return False
    accept_invitation(user=user, organization_id=proof.organization_id, proof=proof, verified_signup=True)
    # setup_user_email runs after save_user; preserve only this request's proof.
    request._skeleton_verified_invitation_email = proof.email
    request.session.pop("skeleton_invitation", None)
    return True


def _link_signup(request):
    """The join link this session opened, when its organization lets people sign up through it (#27)."""
    from skeleton.tenancy.links import pending_join_link

    proof = pending_join_link(request)
    return proof if proof is not None and proof.signup_allowed else None


class AccountAdapter(DefaultAccountAdapter):
    def pre_login(self, request, user, **kwargs):
        # Defense in depth after the backend: no interactive login path admits a service account (#44).
        if getattr(user, "is_service_account", False):
            _deny()
        return super().pre_login(request, user, **kwargs)

    def is_open_for_signup(self, request):
        return (
            pending_invitation(request) is not None
            or getattr(settings, "SKELETON_PUBLIC_SIGNUP", False)
            or _link_signup(request) is not None
        )

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
                raise ValidationError({"email": "Invitation email cannot be changed."})
            if commit:
                user.save()
                ensure_profile(user)
                if not _accept_pending(request, user):
                    link = _link_signup(request)
                    if link is not None:
                        from skeleton.tenancy.links import forget, start_link_signup

                        # Holds one use until the email is confirmed (possibly on another device).
                        start_link_signup(user=user, proof=link)
                        forget(request)
            return user

    def confirm_email(self, request, email_address):
        with transaction.atomic():
            confirmed = super().confirm_email(request, email_address)
            if confirmed and email_address.user.is_active and not email_address.user.deleted_at:
                from skeleton.tenancy.links import finish_link_signup

                if finish_link_signup(email_address.user) is None:
                    _domain_admission(email_address.user, email_address.email)
            if confirmed:
                from allauth.socialaccount.models import SocialAccount

                from skeleton.accounts.providers import configured_providers
                from skeleton.tenancy.context import system_context
                from skeleton.tenancy.models import Membership, Organization

                user = email_address.user
                if not user.is_active or user.deleted_at:
                    _deny()
                for account in SocialAccount.objects.filter(user=user):
                    binding = configured_providers().get(account.provider)
                    pending = account.extra_data.get("skeleton_pending_email")
                    if not (
                        binding
                        and binding.organization_slug
                        and admission_for(binding).jit_provisioning
                        and pending == normalized_email(email_address.email)
                        and account.extra_data.get("skeleton_binding") == binding.fingerprint
                    ):
                        continue
                    organization, role = SocialAccountAdapter()._resolve_binding(binding)
                    with system_context(reason="sso:validated", organization_id=organization.pk):
                        Organization.objects.select_for_update().get(pk=organization.pk)
                        # A pending initial admission is not authority to undo local offboarding.
                        existed = Membership.objects.with_deleted().filter(user=user).exists()
                        account.extra_data = {"skeleton_binding": binding.fingerprint}
                        account.save(update_fields=["extra_data"])
                        if not existed:
                            admit_or_request_social(user, binding)
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
        from allauth.core import context as allauth_context

        from skeleton.common.mail import schedule_mail

        schedule_mail(message, request=allauth_context.request)


class SocialAccountAdapter(DefaultSocialAccountAdapter):
    PROVIDER_CLASSES = {"oidc": "openid_connect", "saml": "saml"}

    def list_apps(self, request, provider=None, client_id=None):
        """File apps plus database providers (#29), built in memory per request, never saved."""
        from allauth.socialaccount.models import SocialApp

        from skeleton.accounts.identity_providers import database_bindings

        apps = super().list_apps(request, provider=provider, client_id=client_id)
        present = {app.provider_id for app in apps if app.provider_id}
        for binding in database_bindings():
            if binding.provider_id in present or binding.protocol not in self.PROVIDER_CLASSES:
                continue  # The file keeps its own app; validate_provider_bindings reports conflicts.
            app = SocialApp(provider=self.PROVIDER_CLASSES[binding.protocol], **binding.app)
            if provider and provider not in (app.provider, app.provider_id):
                continue
            if client_id and app.client_id != client_id:
                continue
            apps.append(app)
        return apps

    def is_email_verified(self, provider, email):
        # Preserve protocol evidence, never fabricate it from operator/domain overrides.
        return False

    def can_authenticate_by_email(self, login, email):
        return False

    def is_open_for_signup(self, request, sociallogin):
        binding = binding_for_sociallogin(sociallogin)
        return (
            pending_invitation(request) is not None
            or bool(binding.organization_slug and admission_for(binding).jit_provisioning)
            or bool(not binding.organization_slug and getattr(settings, "SKELETON_PUBLIC_SIGNUP", False))
        )

    def pre_social_login(self, request, sociallogin):
        from skeleton.accounts.models import User

        if getattr(getattr(request, "user", None), "is_service_account", False) or (
            sociallogin.is_existing and sociallogin.user.is_service_account
        ):
            _deny()  # Service accounts never connect or log in through a provider (#44).
        binding = binding_for_sociallogin(sociallogin)
        connecting = sociallogin.state.get("process") == "connect"
        if connecting:
            from skeleton.accounts.reauthentication import has_recent_authentication

            # SSO-only accounts get no fallback: connecting needs a recent record for this user.
            if not has_recent_authentication(request) or request.user.deleted_at:
                _deny()
        if sociallogin.is_existing:
            user = sociallogin.user
            if user.deleted_at or not user.is_active:
                _deny()
            stored = sociallogin.account.extra_data.get("skeleton_binding")
            if stored != binding.fingerprint:
                _deny()
            if connecting and user.pk != request.user.pk:
                _deny()
        elif not connecting:
            email = normalized_email(sociallogin.user.email) if sociallogin.user.email else ""
            if email and User.all_objects.filter(email=email).exists():
                # Existing emails must log in and explicitly connect after reauthentication.
                _deny()
            if not email and (binding.protocol != "govbr" or not self.is_open_for_signup(request, sociallogin)):
                _deny()
            proof = pending_invitation(request)
            if proof is not None and email and email != proof.email:
                _deny()
            if binding.organization_slug and proof is None:
                allowed = admission_for(binding).allowed_email_domains
                from skeleton.tenancy.domains import email_domain

                if allowed and (not email or email_domain(email) not in allowed):
                    _deny()  # Same neutral answer: the rule itself is never revealed (#31).
        # Trust only assertions already validated by the library and explicitly permitted by config.
        for address in sociallogin.email_addresses:
            address.verified = bool(address.verified and binding.trust_verified_email)
        # Never persist tokens/claims or allow an IdP to promote platform privileges.
        pending_email = (
            sociallogin.account.extra_data.get("skeleton_pending_email") if sociallogin.is_existing else None
        )
        sociallogin.account.extra_data = {"skeleton_binding": binding.fingerprint}
        if isinstance(pending_email, str):
            sociallogin.account.extra_data["skeleton_pending_email"] = pending_email
        sociallogin.user.is_staff = False if not sociallogin.is_existing else sociallogin.user.is_staff
        sociallogin.user.is_superuser = False if not sociallogin.is_existing else sociallogin.user.is_superuser
        # Protocol validation/token use has completed before this hook. Pending signup
        # must not retain credentials or arbitrary protocol state in django_session.
        sociallogin.token = None
        sociallogin.state = {
            key: sociallogin.state[key] for key in ("process", "next", "headless") if key in sociallogin.state
        }
        if binding.organization_slug:
            self._resolve_binding(binding)
        if connecting and binding.protocol == "govbr":
            # Connecting also opens a government browser session. Only a
            # validated, recently authenticated flow can set this provider ID.
            request.session["skeleton_govbr_logout_provider"] = binding.provider_id

    def _resolve_binding(self, binding):
        from skeleton.access.models import Role
        from skeleton.tenancy.context import system_context
        from skeleton.tenancy.models import Organization

        organization = Organization.objects.filter(slug=binding.organization_slug, is_active=True).first()
        if organization is None:
            _deny()
        admission = admission_for(binding)
        with system_context(reason="sso:validated", organization_id=organization.pk):
            # The role comes from the organization's rules or the file, never from a claim.
            if admission.default_role_id is not None:
                role = Role.objects.filter(pk=admission.default_role_id).first()
            else:
                role = Role.objects.filter(code=admission.default_role_code).first()
            if role is None:
                _deny()
            return organization, role

    def save_user(self, request, sociallogin, form=None):
        binding = binding_for_sociallogin(sociallogin)
        with transaction.atomic():
            if not self.is_open_for_signup(request, sociallogin):
                _deny()
            proof = pending_invitation(request)
            email = form.cleaned_data.get("email") if form is not None else sociallogin.user.email
            email = normalized_email(email or "")
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
            if not invited and binding.organization_slug and admission_for(binding).jit_provisioning:
                # Never provision a membership from an unverified email; allauth confirmation can
                # later finish this same binding via email_confirmed without trusting external claims.
                from allauth.account.models import EmailAddress

                if EmailAddress.objects.filter(user=user, email__iexact=user.email, verified=True).exists():
                    admit_or_request_social(user, binding)
                    return user
                else:
                    sociallogin.account.extra_data = {
                        "skeleton_binding": binding.fingerprint,
                        "skeleton_pending_email": normalized_email(user.email),
                    }
                    sociallogin.account.save(update_fields=["extra_data"])
            if not invited:
                from allauth.account.models import EmailAddress

                if EmailAddress.objects.filter(user=user, email__iexact=user.email, verified=True).exists():
                    _domain_admission(user, user.email)
            return user


def admit_or_request_social(user, binding):
    """JIT admission (#29, #31): the membership, or a pending ``sso`` request when approval is on."""
    if admission_for(binding).jit_requires_approval:
        return request_social_admission(user, binding)
    return provision_social_membership(user, binding)


def request_social_admission(user, binding):
    from django.utils import timezone

    from skeleton.tenancy.admission import REQUEST_TTL, effective_status
    from skeleton.tenancy.context import system_context
    from skeleton.tenancy.models import JoinRequest, Membership, Organization

    organization, role = SocialAccountAdapter()._resolve_binding(binding)
    with system_context(reason="sso:validated", organization_id=organization.pk), transaction.atomic():
        Organization.objects.select_for_update().get(pk=organization.pk)
        if Membership.objects.filter(user=user, is_active=True).exists():
            return None
        existing = JoinRequest.objects.select_for_update().filter(user=user, status__in=JoinRequest.OPEN).first()
        if existing is not None and effective_status(existing) != JoinRequest.Status.EXPIRED:
            return existing
        if existing is not None:
            existing.status = JoinRequest.Status.EXPIRED
            existing.save(update_fields={"status"})
        record = JoinRequest(
            organization=organization,
            user=user,
            channel=JoinRequest.Channel.SSO,
            role=role,
            status=JoinRequest.Status.PENDING,
            expires_at=timezone.now() + REQUEST_TTL,
        )
        record.save()
        return record


def provision_social_membership(user, binding):
    from skeleton.access.services import provision_membership
    from skeleton.tenancy.context import system_context

    organization, role = SocialAccountAdapter()._resolve_binding(binding)
    with system_context(reason="sso:validated", organization_id=organization.pk):
        return provision_membership(organization=organization, user=user, role=role)


class EncryptedMFAAdapter(DefaultMFAAdapter):
    def encrypt(self, text):
        return cipher().encrypt(text.encode()).decode()

    def decrypt(self, encrypted_text):
        return cipher().decrypt(encrypted_text.encode()).decode()
