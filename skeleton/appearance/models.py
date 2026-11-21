"""The global tables of the instance appearance (#592, #622).

``InstanceTheme`` is one named theme of the catalog (colour, radius, font and custom CSS);
``InstanceBrand`` is the single row with the product name, the footer, the three image keys and
the active theme. Both are written only by ``skeleton.appearance.services`` and
``skeleton.appearance.images``, for the platform administrator; nothing here is tenant data.
``InstancePage`` is the public home page of the platform (#622), written only by
``skeleton.appearance.pages``.
"""

from django.db import models
from django.db.models import Q

from skeleton.common.models import SystemRecord
from skeleton.tenancy.models import BRAND_COLOR_PATTERN

RADII = ("sharp", "default", "round")
FONTS = ("inter", "system", "serif")
BRAND_SLOT = "instance"


class InstanceTheme(SystemRecord):
    """A named theme of the catalog; at most one theme (or one built-in) is active at a time."""

    name = models.CharField(max_length=80)
    # "" keeps the core colour; otherwise #rrggbb with AA contrast against white (validated by the service).
    brand_color = models.CharField(max_length=7, blank=True, default="")
    radius = models.CharField(max_length=8, default="default")
    font = models.CharField(max_length=8, default="inter")
    # Up to 32 KiB, validated by skeleton.appearance.css_validation; never embedded in HTML.
    custom_css = models.TextField(blank=True, default="")

    class Meta(SystemRecord.Meta):
        constraints = SystemRecord.Meta.constraints + [
            models.UniqueConstraint(
                fields=("name",),
                condition=Q(deleted_at__isnull=True),
                name="sk_appearance_theme_name_live",
                violation_error_code="unique",
            ),
            models.CheckConstraint(
                condition=Q(brand_color="") | Q(brand_color__regex=BRAND_COLOR_PATTERN),
                name="sk_appearance_theme_brand_color",
            ),
            models.CheckConstraint(condition=Q(radius__in=RADII), name="sk_appearance_theme_radius"),
            models.CheckConstraint(condition=Q(font__in=FONTS), name="sk_appearance_theme_font"),
        ]

    def __str__(self):
        return self.name


class InstanceBrand(SystemRecord):
    """The single brand row of the instance (``slot = "instance"``); absent means the core brand."""

    slot = models.CharField(max_length=8, default=BRAND_SLOT, unique=True)
    product_name = models.CharField(max_length=60, blank=True, default="")  # "" shows "Skeleton".
    footer_text = models.CharField(max_length=160, blank=True, default="")
    # Private storage keys platform/branding/<uuid4>: never shown, redacted in the audit.
    logo_key = models.CharField(max_length=300, blank=True, default="")
    logo_dark_key = models.CharField(max_length=300, blank=True, default="")
    icon_key = models.CharField(max_length=300, blank=True, default="")
    # One of BUILTIN_THEMES, or a theme of the catalog, or neither (the core theme); never both.
    builtin_theme = models.CharField(max_length=32, blank=True, default="")
    active_theme = models.ForeignKey(InstanceTheme, null=True, blank=True, on_delete=models.PROTECT, related_name="+")

    class Meta(SystemRecord.Meta):
        constraints = SystemRecord.Meta.constraints + [
            models.CheckConstraint(condition=Q(slot=BRAND_SLOT), name="sk_appearance_brand_single_slot"),
            models.CheckConstraint(
                condition=Q(builtin_theme="") | Q(active_theme__isnull=True),
                name="sk_appearance_brand_one_theme",
            ),
        ]

    def __str__(self):
        return self.product_name or "Skeleton"


HOME_SLOT = "home"


class InstancePage(SystemRecord):
    """The public home page of the instance (#622): one row per slot, only ``slot = "home"`` today.

    ``document`` is a page of structured blocks validated by ``skeleton.common.page_blocks``
    (``kind="platform_home"``): plain text, never HTML. Shown at ``/`` of the platform host to
    whoever has not signed in, only while ``published``. Written only by
    ``skeleton.appearance.pages.save_home_page``, for the platform administrator, with CAS.
    """

    slot = models.CharField(max_length=16, default=HOME_SLOT, unique=True)
    document = models.JSONField(default=dict, blank=True)
    published = models.BooleanField(default=False)

    class Meta(SystemRecord.Meta):
        constraints = SystemRecord.Meta.constraints + [
            models.CheckConstraint(condition=Q(slot=HOME_SLOT), name="sk_appearance_page_home_slot"),
        ]

    def __str__(self):
        return self.slot
