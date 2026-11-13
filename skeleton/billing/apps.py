from django.apps import AppConfig
from django.utils.translation import gettext_lazy


class BillingConfig(AppConfig):
    name = "skeleton.billing"
    label = "sk_billing"
    verbose_name = gettext_lazy("Cobrança")
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from django.conf import settings

        from skeleton.common.entitlements import register_exempt_segment, register_policy
        from skeleton.common.organization_hooks import register_organization_created

        from . import checks  # noqa: F401 - registers the billing checks
        from .dunning import register_dunning
        from .entitlements import POLICY
        from .events import register_billing_events
        from .jobs import register_billing_jobs
        from .lifecycle import organization_created
        from .notices import register_notices
        from .quota import register_quota
        from .vouchers import register_voucher_lines

        # The policy is always registered and answers "allow everything" while billing is off,
        # so a setting override in tests or at runtime needs no new registration.
        register_policy(POLICY)
        register_exempt_segment("billing")
        register_organization_created(organization_created)
        register_billing_events()
        register_voucher_lines()
        register_quota()
        register_dunning()
        register_notices()
        register_billing_jobs(enabled=bool(getattr(settings, "SKELETON_BILLING_ENABLED", False)))
