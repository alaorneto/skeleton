"""No model is registered in the Django admin.

Global identities, organizations, the table catalog and the global audit are managed by the
explicit views of ``skeleton.web.platform`` (/platform/), which call the lifecycle services
(CAS, last-administrator guard, ``auth_version``). Generic ModelAdmin editors would bypass
those services, so none is offered; library models are unregistered in ``admin_config``.
"""
