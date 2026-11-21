"""``{% appearance_cached as brand %}`` (#598): the instance brand from process memory only.

For the standalone error pages, which render without a context and possibly with the database
down (500): never a query, never ``request``. Before any page warmed the cache it is the core brand.
"""

from django import template

register = template.Library()


@register.simple_tag
def appearance_cached():
    from skeleton.appearance.snapshot import cached_appearance
    from skeleton.web.instance_brand import instance_brand

    return instance_brand(cached_appearance())
