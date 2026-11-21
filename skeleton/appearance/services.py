"""Writes of the instance appearance (#592, #595): only the platform administrator, always with CAS.

Every function is keyword-only and runs in ``transaction.atomic()``; the process cache is reset
after the commit. ``actor=None`` is reserved for the console (``manage.py appearance``), which
opens its own ``system_context`` with a reason; HTTP views always pass ``request.user``.
Closed field lists: anything else is refused with "Campo não permitido.".
"""

from __future__ import annotations

from typing import Any

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.utils.translation import gettext

from skeleton.common.exceptions import ConcurrentUpdate

from .snapshot import reset_appearance_cache

BRAND_FIELDS = {"product_name": 60, "footer_text": 160}
THEME_FIELDS = ("name", "brand_color", "radius", "font", "custom_css")
MAX_THEMES = 50


def _require_platform(actor: Any) -> None:
    from skeleton.accounts.platform import is_platform_admin

    if actor is not None and not is_platform_admin(actor):
        raise PermissionDenied("Platform administration required.")


def _check_revision(expected_revision: Any) -> None:
    if type(expected_revision) is not int:
        raise ConcurrentUpdate("A revision is required.")


def current_brand() -> Any:
    from .models import InstanceBrand

    return InstanceBrand.objects.filter(slot="instance").first()


def revision_of(record: Any) -> int:
    return record.revision if record else 0


def _refuse_unknown(data: Any, allowed: Any) -> None:
    if not isinstance(data, dict):
        raise ValidationError(gettext("Dados inválidos."))
    unknown = set(data) - set(allowed)
    if unknown:
        raise ValidationError({name: gettext("Campo não permitido.") for name in sorted(unknown)})


def _line(name: str, value: Any, limit: int, *, required: bool = False) -> str:
    if not isinstance(value, str):
        raise ValidationError({name: gettext("Use um texto.")})
    value = value.strip()
    if any(char in value for char in "\n\r\x00"):
        raise ValidationError({name: gettext("Use uma única linha, sem quebras.")})
    if len(value) > limit:
        raise ValidationError({name: gettext("Use até %(limit)d caracteres.") % {"limit": limit}})
    if required and not value:
        raise ValidationError({name: gettext("Informe um nome.")})
    return value


def _locked_brand(expected_revision: Any) -> Any:
    """The brand row under ``select_for_update`` after the CAS check (``None`` with revision 0)."""
    from .models import InstanceBrand

    _check_revision(expected_revision)
    record = InstanceBrand.objects.select_for_update().filter(slot="instance").first()
    if revision_of(record) != expected_revision:
        raise ConcurrentUpdate("Instance brand changed.")
    return record


def _create_brand(**values: Any) -> Any:
    """Create the single row; a concurrent creator wins and this caller gets ``ConcurrentUpdate``."""
    from .models import InstanceBrand

    record = InstanceBrand(**values)
    try:
        with transaction.atomic():
            record.save()
    except IntegrityError:
        raise ConcurrentUpdate("Instance brand created concurrently.") from None
    return record


def update_brand(*, actor: Any, data: dict[str, Any], expected_revision: int) -> Any:
    """Change the product name and the footer text; equal values write nothing."""
    _require_platform(actor)
    _refuse_unknown(data, BRAND_FIELDS)
    values = {name: _line(name, value, BRAND_FIELDS[name]) for name, value in data.items()}
    with transaction.atomic():
        record = _locked_brand(expected_revision)
        if record is None:
            if not any(values.values()):
                return None
            record = _create_brand(**values)
        else:
            changed = {name for name, value in values.items() if getattr(record, name) != value}
            for name in changed:
                setattr(record, name, values[name])
            if not changed:
                return record
            record.save(update_fields=changed)
        transaction.on_commit(reset_appearance_cache)
    return record


# --------------------------------------------------------------------------- themes (#595)


def clean_theme_values(data: dict[str, Any]) -> dict[str, Any]:
    """The validated subset of ``data``; errors name the field."""
    from skeleton.tenancy.branding import validate_brand_color

    from .css_validation import validate_custom_css
    from .models import FONTS, RADII

    _refuse_unknown(data, THEME_FIELDS)
    values: dict[str, Any] = {}
    if "name" in data:
        values["name"] = _line("name", data["name"], 80, required=True)
    if "brand_color" in data:
        color = data["brand_color"]
        if not isinstance(color, str):
            raise ValidationError({"brand_color": gettext("Use uma cor no formato #RRGGBB.")})
        color = color.strip().lower()
        validate_brand_color(color)
        values["brand_color"] = color
    if "radius" in data:
        if data["radius"] not in RADII:
            raise ValidationError({"radius": gettext("Escolha um raio da lista.")})
        values["radius"] = data["radius"]
    if "font" in data:
        if data["font"] not in FONTS:
            raise ValidationError({"font": gettext("Escolha uma fonte da lista.")})
        values["font"] = data["font"]
    if "custom_css" in data:
        values["custom_css"] = validate_custom_css(data["custom_css"])
    return values


def _name_taken() -> ValidationError:
    return ValidationError({"name": gettext("Já existe um tema com esse nome.")})


def _save_theme(record: Any, **kwargs: Any) -> None:
    try:
        with transaction.atomic():
            record.save(**kwargs)
    except IntegrityError:
        raise _name_taken() from None


def list_themes() -> list[Any]:
    """Live themes of the catalog, by name."""
    from .models import InstanceTheme

    return list(InstanceTheme.objects.order_by("name", "sysid"))


def get_theme(theme_id: Any) -> Any:
    """A live theme or ``None``."""
    from .models import InstanceTheme

    try:
        return InstanceTheme.objects.filter(pk=theme_id).first()
    except (ValueError, ValidationError):
        return None


def create_theme(*, actor: Any, data: dict[str, Any]) -> Any:
    from .models import InstanceTheme

    _require_platform(actor)
    values = clean_theme_values(data)
    if "name" not in values:
        raise ValidationError({"name": gettext("Informe um nome.")})
    with transaction.atomic():
        if InstanceTheme.objects.count() >= MAX_THEMES:
            raise ValidationError(gettext("O catálogo pode ter até %(count)d temas.") % {"count": MAX_THEMES})
        if InstanceTheme.objects.filter(name=values["name"]).exists():
            raise _name_taken()
        record = InstanceTheme(**values)
        _save_theme(record)
        transaction.on_commit(reset_appearance_cache)
    return record


def _locked_theme(theme_id: Any, expected_revision: Any) -> Any:
    from .models import InstanceTheme

    _check_revision(expected_revision)
    try:
        record = InstanceTheme.objects.select_for_update().filter(pk=theme_id).first()
    except (ValueError, ValidationError):
        record = None
    if record is None:
        raise ValidationError({"theme": gettext("Tema inexistente.")})
    if record.revision != expected_revision:
        raise ConcurrentUpdate("Theme changed.")
    return record


def update_theme(*, actor: Any, theme_id: Any, data: dict[str, Any], expected_revision: int) -> Any:
    from .models import InstanceTheme

    _require_platform(actor)
    values = clean_theme_values(data)
    with transaction.atomic():
        record = _locked_theme(theme_id, expected_revision)
        if record.source == "package":
            raise ValidationError(
                gettext("Temas de pacote não são editados aqui; envie uma versão nova do pacote."),
                code="package_read_only",
            )
        changed = {name for name, value in values.items() if getattr(record, name) != value}
        if "name" in changed and InstanceTheme.objects.filter(name=values["name"]).exclude(pk=record.pk).exists():
            raise _name_taken()
        for name in changed:
            setattr(record, name, values[name])
        if changed:
            _save_theme(record, update_fields=changed)
            transaction.on_commit(reset_appearance_cache)
    return record


def delete_theme(*, actor: Any, theme_id: Any, expected_revision: int) -> Any:
    """Soft delete; the active theme is refused (the brand row is locked before the check)."""
    from .models import InstanceBrand

    _require_platform(actor)
    with transaction.atomic():
        brand = InstanceBrand.objects.select_for_update().filter(slot="instance").first()
        record = _locked_theme(theme_id, expected_revision)
        if brand is not None and brand.active_theme_id == record.pk:
            raise ValidationError({"theme": gettext("Este é o tema ativo. Ative outro tema antes de excluí-lo.")})
        record.delete()
        transaction.on_commit(reset_appearance_cache)
    return record


def activate_theme(*, actor: Any, builtin: str = "", theme_id: Any = None, expected_revision: int) -> Any:
    """Make a built-in or a catalog theme active (neither: the core theme); CAS on the brand row."""
    from .themes import BUILTIN_THEMES

    _require_platform(actor)
    if builtin and theme_id is not None:
        raise ValidationError({"theme": gettext("Escolha um tema só.")})
    if builtin and (not isinstance(builtin, str) or builtin not in BUILTIN_THEMES):
        raise ValidationError({"theme": gettext("Tema pronto inexistente.")})
    with transaction.atomic():
        record = _locked_brand(expected_revision)
        theme = None
        if theme_id is not None:
            theme = get_theme(theme_id)
            if theme is None:
                raise ValidationError({"theme": gettext("Tema inexistente.")})
        values = {"builtin_theme": builtin or "", "active_theme": theme}
        if record is None:
            if not builtin and theme is None:
                return None
            record = _create_brand(**values)
        else:
            if record.builtin_theme == values["builtin_theme"] and record.active_theme_id == (
                theme.pk if theme is not None else None
            ):
                return record
            record.builtin_theme = values["builtin_theme"]
            record.active_theme = theme
            record.save(update_fields={"builtin_theme", "active_theme"})
        transaction.on_commit(reset_appearance_cache)
    return record


def active_theme_values() -> Any:
    """``ThemeValues`` of the active theme, or ``None`` for the core theme."""
    from .themes import BUILTIN_THEMES, ThemeValues

    brand = current_brand()
    if brand is None:
        return None
    if brand.active_theme_id:
        theme = get_theme(brand.active_theme_id)
        if theme is not None:
            return ThemeValues(theme.name, theme.brand_color, theme.radius, theme.font, theme.custom_css)
        return None
    return BUILTIN_THEMES.get(brand.builtin_theme) if brand.builtin_theme else None
