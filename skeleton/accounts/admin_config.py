from django.contrib.admin.apps import AdminConfig


class PlatformAdminConfig(AdminConfig):
    default_site = 'skeleton.accounts.admin_site.PlatformAdminSite'

    def ready(self):
        super().ready()
        from django.apps import apps
        from django.contrib import admin
        # These libraries retain their own lifecycle; no generic editor for credentials.
        for label in ("auth.Group", "account.EmailAddress", "socialaccount.SocialAccount",
                      "socialaccount.SocialApp", "socialaccount.SocialToken", "mfa.Authenticator"):
            model = apps.get_model(label)
            if admin.site.is_registered(model):
                admin.site.unregister(model)
