from allauth.account.auth_backends import AuthenticationBackend


class SkeletonAuthenticationBackend(AuthenticationBackend):
    def user_can_authenticate(self, user):
        return bool(super().user_can_authenticate(user) and user.deleted_at is None and user.local_password_allowed)

    def get_user(self, user_id):
        # SSO-only identities may retain a session on this backend, but never authenticate by password.
        from django.contrib.auth import get_user_model
        try:
            user = get_user_model().objects.get(pk=user_id)
        except get_user_model().DoesNotExist:
            return None
        return user if user.is_active and user.deleted_at is None else None

    def get_user_permissions(self, user_obj, obj=None):
        return set()

    def get_group_permissions(self, user_obj, obj=None):
        return set()

    def get_all_permissions(self, user_obj, obj=None):
        return set()

    def has_perm(self, user_obj, perm, obj=None):
        return bool(user_obj.is_active and user_obj.deleted_at is None and user_obj.is_superuser)

    def has_module_perms(self, user_obj, app_label):
        return self.has_perm(user_obj, app_label)
