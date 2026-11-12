"""Brand colour contrast and palette (#168)."""

import pytest
from django.core.exceptions import ValidationError

from skeleton.tenancy.branding import AA, DARK_CANVAS, WHITE, _rgb, contrast, palette, validate_brand_color


def test_contrast_matches_wcag_reference_values():
    assert contrast((0, 0, 0), WHITE) == pytest.approx(21.0)
    assert contrast(WHITE, WHITE) == pytest.approx(1.0)
    assert contrast(_rgb("#767676"), WHITE) == pytest.approx(4.54, abs=0.01)


@pytest.mark.parametrize("color", ["#1d4ed8", "#000000", "#767676", ""])
def test_accessible_colours_pass(color):
    validate_brand_color(color)


@pytest.mark.parametrize("color", ["#ffff00", "#777777", "#60a5fa"])
def test_low_contrast_colours_are_refused(color):
    with pytest.raises(ValidationError) as error:
        validate_brand_color(color)
    assert error.value.error_dict["brand_color"][0].code == "contrast"


@pytest.mark.parametrize("color", ["#1d4ed8", "#000000", "#7c2d12", "#065f46", "#767676"])
def test_palette_keeps_aa_in_both_themes(color):
    tokens = palette(color)
    assert tokens is not None
    assert contrast(_rgb(tokens.brand), WHITE) >= AA
    assert contrast(_rgb(tokens.brand_strong), WHITE) >= AA
    assert contrast(_rgb(tokens.text_dark), DARK_CANVAS) >= AA
    assert contrast(_rgb(tokens.text_dark_strong), DARK_CANVAS) >= AA


def test_palette_css_carries_only_hex_values():
    css = palette("#1d4ed8").css()
    assert css.startswith(":root{--color-brand:#1d4ed8;")
    assert "@media (prefers-color-scheme: dark)" in css
    assert all(char not in css for char in "<>\"'\\")


@pytest.mark.parametrize("color", ["", "#ffff00", "red", "#1d4ed8;}body{display:none", "</style>"])
def test_no_palette_for_blank_invalid_or_low_contrast(color):
    assert palette(color) is None
