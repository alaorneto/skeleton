import logging
from django.contrib import messages
from django.db import transaction
from skeleton.tenancy.context import current_context
from skeleton.common.logging import security_event


def schedule_mail(message, *, request=None, target_sysid=None):
    # Validate/materialize headers before commit; callback receives no lazy ORM state.
    message.message()
    context = current_context()
    request = getattr(request, '_request', request)

    def deliver():
        try:
            if message.send(fail_silently=False) != 1:
                raise RuntimeError('Email backend did not accept the message.')
        except Exception as error:
            security_event('mail.delivery_failed', level=logging.ERROR, context_snapshot=context,
                           error_class=type(error).__name__, target_sysid=str(target_sysid) if target_sysid else None)
            if request is not None:
                request._skeleton_mail_failures = getattr(request, '_skeleton_mail_failures', 0) + 1
                if hasattr(request, '_messages'):
                    messages.warning(request, 'Dados salvos, mas o email não foi entregue. Use o reenvio explícito.')

    transaction.on_commit(deliver)
