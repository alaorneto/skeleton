from allauth.account.forms import ChangePasswordForm, ResetPasswordForm, ResetPasswordKeyForm, SetPasswordForm, SignupForm
from allauth.socialaccount.forms import SignupForm as SocialSignupForm
from allauth.core import context
from django import forms

from skeleton.accounts.services import password_allowed
from skeleton.tenancy.invitations import pending_invitation


class InvitationSignupForm(SignupForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        request = context.request
        proof = pending_invitation(request) if request is not None else None
        if proof is not None:
            self.initial['email'] = proof.email
            self.fields['email'].disabled = True

    def clean_email(self):
        email = super().clean_email()
        request = context.request
        proof = pending_invitation(request) if request is not None else None
        if proof is not None and email.strip().lower() != proof.email:
            raise forms.ValidationError('Invitation email cannot be changed.')
        return email


class InvitationSocialSignupForm(SocialSignupForm):
    """Collect a real email through allauth without changing invitation scope."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        request = context.request
        proof = pending_invitation(request) if request is not None else None
        if proof is not None:
            self.initial['email'] = proof.email
            self.fields['email'].disabled = True

    def clean_email(self):
        email = super().clean_email()
        request = context.request
        proof = pending_invitation(request) if request is not None else None
        if proof is not None and email.strip().lower() != proof.email:
            raise forms.ValidationError('Invitation email cannot be changed.')
        return email


class PasswordPolicyMixin:
    def clean(self):
        data = super().clean()
        if self.user is None or not password_allowed(self.user):
            raise forms.ValidationError('Local password authentication is unavailable.')
        return data

    def save(self, *args, **kwargs):
        # Recheck at use, not only when a reset token was issued or form was first displayed.
        if self.user is None or not password_allowed(self.user):
            raise forms.ValidationError('Local password authentication is unavailable.')
        return super().save(*args, **kwargs)


class PolicyResetPasswordForm(ResetPasswordForm):
    def clean_email(self):
        email = super().clean_email()
        self.users = [user for user in self.users if password_allowed(user)]
        return email


class PolicyResetPasswordKeyForm(PasswordPolicyMixin, ResetPasswordKeyForm):
    pass


class PolicyChangePasswordForm(PasswordPolicyMixin, ChangePasswordForm):
    pass


class PolicySetPasswordForm(PasswordPolicyMixin, SetPasswordForm):
    pass
