from uuid import uuid4
from .logging import security_event


def outcome(status):
    if status >= 500:
        return 'server_error'
    if status in {409, 412, 428}:
        return 'conflict'
    if status in {401, 403}:
        return 'access_denied'
    if status == 404:
        return 'not_found'
    if status >= 400:
        return 'validation_error'
    return 'success'


class RequestCompletionMiddleware:
    """Outer middleware observes the response after transactions and commit callbacks."""
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.request_id = uuid4()
        response = self.get_response(request)
        response['X-Request-ID'] = str(request.request_id)
        failed_delivery = bool(getattr(request, '_skeleton_mail_failures', 0))
        if failed_delivery:
            response['X-Email-Delivery'] = 'failed'
        security_event('http.request', status=response.status_code, outcome=outcome(response.status_code),
                       request_id=str(request.request_id), source='http',
                       context_snapshot=getattr(request, '_skeleton_log_context', None),
                       reason_code='email_delivery_failed' if failed_delivery else None)
        return response
