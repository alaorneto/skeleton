"""Template context of the platform pages: the lazy update notice (#501)."""

from django.utils.functional import SimpleLazyObject

from skeleton.accounts.platform import UPDATES_OPERATE, has_platform_capability


def platform_update_notice(request):
    """Lazy update notice for /platform/ pages; empty elsewhere and for users without updates.operate."""
    user = getattr(request, "user", None)
    if (
        not getattr(request, "path", "").startswith("/platform/")
        or not getattr(user, "is_authenticated", False)
        or not has_platform_capability(user, UPDATES_OPERATE)
    ):
        return {}
    from skeleton.system.releases import update_notice

    return {"platform_update_notice": SimpleLazyObject(lambda: update_notice() or {})}


def platform_notifications(request):
    """Lazy unread count of the platform's inbox (#787): read only when a template shows it."""
    user = getattr(request, "user", None)
    if not getattr(user, "is_authenticated", False):
        return {}

    memo: dict[str, int] = {}

    def count():
        if "n" not in memo:
            from skeleton.notifications.platform import unread_count

            memo["n"] = unread_count(user)
        return memo["n"]

    def label():
        from skeleton.notifications.inbox import counter_label

        return counter_label(count())

    return {"platform_unread": SimpleLazyObject(count), "platform_unread_label": SimpleLazyObject(label)}
