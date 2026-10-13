from django.contrib.auth import logout


class AuthVersionMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = request.user
        if user.is_authenticated and (not user.is_active or user.deleted_at is not None or request.session.get('skeleton_auth_version') != user.auth_version):
            logout(request)
        return self.get_response(request)
