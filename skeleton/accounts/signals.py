import logging

from allauth.account import signals as account_signals
from allauth.mfa import signals as mfa_signals
from django.contrib.auth.signals import user_logged_in, user_logged_out, user_login_failed
from django.dispatch import receiver

from skeleton.accounts.services import revoke_sessions

logger = logging.getLogger('skeleton.security')


@receiver(user_logged_in, dispatch_uid='skeleton.session.version')
def remember_auth_version(sender, request, user, **kwargs):
    request.session['skeleton_auth_version'] = user.auth_version
    from skeleton.tenancy.context import refresh_authenticated_actor
    refresh_authenticated_actor(user)
    logger.info('login_succeeded', extra={'event': 'login_succeeded', 'target_sysid': str(user.pk)})


@receiver(user_logged_out, dispatch_uid='skeleton.session.logout')
def logout_context(sender, request, user, **kwargs):
    from skeleton.tenancy.context import refresh_authenticated_actor
    refresh_authenticated_actor(None)
    logger.info('logout', extra={'event': 'logout', 'target_sysid': str(user.pk) if user else None})


@receiver(user_login_failed, dispatch_uid='skeleton.login.failure')
def failed_login(sender, credentials, request, **kwargs):
    # credentials may contain plaintext passwords: deliberately never log them.
    logger.warning('login_failed', extra={'event': 'login_failed', 'status': 403})


@receiver(mfa_signals.authenticator_added, dispatch_uid='skeleton.mfa.added')
@receiver(mfa_signals.authenticator_removed, dispatch_uid='skeleton.mfa.removed')
@receiver(mfa_signals.authenticator_reset, dispatch_uid='skeleton.mfa.reset')
def invalidate_mfa_sessions(sender, user, request=None, **kwargs):
    revoke_sessions(user)
    if request is not None and request.user.is_authenticated and request.user.pk == user.pk:
        request.session['skeleton_auth_version'] = user.auth_version
    logger.info('mfa_changed', extra={'event': 'mfa_changed', 'target_sysid': str(user.pk)})


@receiver(mfa_signals.authentication_failed, dispatch_uid='skeleton.mfa.failure')
def failed_mfa(sender, **kwargs):
    logger.warning('mfa_failed', extra={'event': 'mfa_failed', 'status': 403})


@receiver(account_signals.password_changed, dispatch_uid='skeleton.password.changed')
@receiver(account_signals.password_set, dispatch_uid='skeleton.password.set')
@receiver(account_signals.password_reset, dispatch_uid='skeleton.password.reset')
def password_event(sender, user, request=None, **kwargs):
    # AccountAdapter.set_password already increments auth_version atomically with password.
    if request is not None and request.user.is_authenticated and request.user.pk == user.pk:
        request.session['skeleton_auth_version'] = user.auth_version
    logger.info('password_changed', extra={'event': 'password_changed', 'target_sysid': str(user.pk)})




@receiver(account_signals.email_changed, dispatch_uid='skeleton.email.synchronized')
def synchronize_primary_email(sender, user, to_email_address, from_email_address=None, request=None, **kwargs):
    from skeleton.accounts.services import normalized_email
    if not to_email_address.verified or to_email_address.user_id != user.pk:
        return
    if from_email_address is not None and normalized_email(from_email_address.email) == normalized_email(to_email_address.email):
        return
    # allauth already synchronized email before emitting this transition signal.
    revoke_sessions(user)
    if request is not None and request.user.is_authenticated and request.user.pk == user.pk:
        request.session['skeleton_auth_version'] = user.auth_version
