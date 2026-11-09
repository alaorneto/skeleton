"""Template filters over the single platform administrator predicate."""

from django import template

from skeleton.accounts import platform

register = template.Library()


@register.filter
def is_platform_admin(user):
    return platform.is_platform_admin(user)


@register.filter
def has_partial_platform_flags(user):
    return platform.has_partial_platform_flags(user)
