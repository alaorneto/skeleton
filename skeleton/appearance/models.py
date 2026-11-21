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
SOURCES = ("form", "package")
BRAND_SLOT = "instance"


class InstanceTheme(SystemRecord):
    """A named theme of the catalog; at most one theme (or one built-in) is active at a time.

    ``source="form"`` is a theme made in /platform/appearance/; ``source="package"`` was installed
    from a theme package (#635): read-only for the form, updated by sending a newer version, and
    its files (logos, icon, fonts) live in private storage under the keys of ``package_files``.
    """

    name = models.CharField(max_length=80)
    # "" keeps the core colour; otherwise #rrggbb with AA contrast against white (validated by the service).
    brand_color = models.CharField(max_length=7, blank=True, default="")
    radius = models.CharField(max_length=8, default="default")
    font = models.CharField(max_length=8, default="inter")
    # Up to 32 KiB, validated by skeleton.appearance.css_validation; never embedded in HTML.
    custom_css = models.TextField(blank=True, default="")
    source = models.CharField(max_length=8, default="form")
    package_id = models.CharField(max_length=128, blank=True, default="")
    package_version = models.CharField(max_length=64, blank=True, default="")
    package_digest = models.CharField(max_length=64, blank=True, default="")  # content_digest of the reader.
    package_manifest = models.JSONField(default=dict, blank=True)  # The validated theme.json, never bytes.
    # Private storage keys platform/branding/<uuid4> of the package files: sensitive, redacted in the audit.
    package_files = models.JSONField(default=dict, blank=True)
    font_family = models.CharField(max_length=64, blank=True, default="")
    product_name = models.CharField(max_length=60, blank=True, default="")
    footer_text = models.CharField(max_length=160, blank=True, default="")

    class Meta(SystemRecord.Meta):
        constraints = SystemRecord.Meta.constraints + [
            models.UniqueConstraint(
                fields=("package_id",),
                condition=Q(deleted_at__isnull=True, source="package"),
                name="sk_appearance_theme_package_live",
                violation_error_code="unique",
            ),
            models.CheckConstraint(condition=Q(source__in=SOURCES), name="sk_appearance_theme_source"),
            models.CheckConstraint(
                condition=Q(source="package")
                | Q(
                    package_id="",
                    package_version="",
                    package_digest="",
                    font_family="",
                    product_name="",
                    footer_text="",
                    package_files={},
                    package_manifest={},
                ),
                name="sk_appearance_theme_form_has_no_package",
            ),
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
