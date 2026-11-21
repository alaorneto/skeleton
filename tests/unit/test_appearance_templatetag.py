"""``{% appearance_cached %}`` (#598) reads process memory only: it renders with no database at all."""

from django.template import Context, Template

from skeleton.appearance.snapshot import Appearance, pin_appearance
from skeleton.web.instance_brand import instance_brand

TEMPLATE = (
    "{% load appearance %}{% appearance_cached as brand %}{{ brand.name }}|{{ brand.theme_url }}|{{ brand.logo_url }}"
)


def test_the_tag_needs_no_database_and_no_request():
    # pytest-django blocks the database in tests/unit: any query would raise here.
    pin_appearance(Appearance(product_name="Acme", theme_version="abc123abc123", logo_version="0123456789ab"))
    html = Template(TEMPLATE).render(Context())
    assert html == "Acme|/brand/theme.css?v=abc123abc123|/brand/logo/?v=0123456789ab"


def test_an_unpinned_cold_cache_is_the_core_brand_without_a_query():
    from skeleton.appearance.snapshot import reset_appearance_cache

    pin_appearance(None)
    reset_appearance_cache()
    assert Template(TEMPLATE).render(Context()) == "||"


def test_without_a_brand_everything_is_empty():
    pin_appearance(Appearance())
    assert Template(TEMPLATE).render(Context()) == "||"


def test_safe_mode_and_the_dark_logo_only_with_the_logo():
    appearance = Appearance(theme_version="aaa", theme_safe_version="bbb", logo_dark_version="ccc")
    assert instance_brand(appearance, safe=True)["theme_url"] == "/brand/theme.css?v=bbb&safe=1"
    assert instance_brand(appearance)["logo_dark_url"] == ""
    both = Appearance(logo_version="ddd", logo_dark_version="ccc")
    assert instance_brand(both)["logo_dark_url"] == "/brand/logo-dark/?v=ccc"
    assert instance_brand(Appearance(theme_version="aaa"), safe=True)["theme_url"] == ""
