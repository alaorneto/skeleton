"""CSS of a theme from colour, radius and font (#593): scale, AA contrast in both modes, determinism."""

import re
from pathlib import Path

from skeleton.appearance.themes import (
    BUILTIN_THEMES,
    FONT_TOKENS,
    RADIUS_TOKENS,
    ThemeValues,
    dark_tokens,
    scale,
    theme_css,
    theme_css_tokens,
    theme_digest,
)
from skeleton.tenancy.branding import AA, DARK_CANVAS, WHITE, _rgb, contrast, validate_brand_color

COLORS = ["#047857", "#374151", "#9f1239", "#6d28d9", "#1d4ed8", "#000000", "#b91c1c", "#0f766e"]
SITE_CSS = Path(__file__).resolve().parents[2] / "skeleton" / "web" / "static" / "skeleton" / "web" / "site.css"


def test_scale_and_contrast_of_each_colour(subtests):
    for color in COLORS:
        with subtests.test(color=color):
            steps = {step: _rgb(value) for step, value in scale(color).items()}
            assert list(steps) == [50, 100, 200, 300, 400, 500, 600, 700, 800, 900, 950]
            assert scale(color)[600] == color
            for step in (600, 700, 800):
                assert contrast(WHITE, steps[step]) >= AA
            assert contrast(steps[700], WHITE) >= AA
            assert contrast(steps[700], steps[50]) >= AA
            dark = dark_tokens(color)
            fg = _rgb(dark["--color-fg-brand"])
            assert contrast(fg, DARK_CANVAS) >= AA
            assert contrast(fg, _rgb(dark["--color-brand-softer"])) >= AA
            assert dark["--color-focus"] == scale(color)[400]


def test_the_generated_part_holds_only_hex_values_and_fixed_literals(subtests):
    fixed = sorted({*RADIUS_TOKENS.values(), *FONT_TOKENS.values()}, key=len, reverse=True)
    for color in COLORS:
        for radius in ("sharp", "default", "round"):
            for font in ("inter", "system", "serif"):
                values = ThemeValues("Nome <secreto>", brand_color=color, radius=radius, font=font)
                with subtests.test(color=color, radius=radius, font=font):
                    css = theme_css_tokens(values)
                    assert "secreto" not in css and "Nome" not in css
                    rest = css
                    for literal in fixed:
                        rest = rest.replace(literal, "")
                    rest = re.sub(r"--color-[a-z0-9-]+:#[0-9a-f]{6}", "", rest)
                    rest = rest.replace("@media (prefers-color-scheme: dark)", "").replace(":root", "")
                    assert re.fullmatch(r"[;{}]*", rest), rest


def test_same_input_same_output_and_core_theme_is_empty():
    values = ThemeValues("A", brand_color="#047857", radius="round", font="serif", custom_css=".x{color:red}")
    assert theme_css(values) == theme_css(ThemeValues("B", "#047857", "round", "serif", ".x{color:red}"))
    assert theme_digest(theme_css(values)) == theme_digest(theme_css(values))
    assert len(theme_digest(theme_css(values))) == 12
    assert theme_css(ThemeValues("Padrão")) == ""
    assert theme_digest("") == ""
    assert theme_css(BUILTIN_THEMES["cobalto"]) == ""


def test_order_of_blocks_and_free_css_last():
    values = ThemeValues("A", brand_color="#9f1239", radius="sharp", font="system", custom_css=".card{padding:0}")
    css = theme_css(values)
    root = css.index(":root{")
    dark = css.index("@media (prefers-color-scheme: dark)")
    custom = css.index(".card{padding:0}")
    assert root < dark < custom
    assert css.endswith(".card{padding:0}")
    assert "--radius-base:.125rem;--radius-card:.25rem" in css
    assert "--font-sans:ui-sans-serif" in css
    assert ".card" not in theme_css_tokens(values)


def test_radius_and_font_without_colour_have_no_dark_block():
    css = theme_css(ThemeValues("A", radius="round", font="serif"))
    assert css == ":root{--radius-base:.75rem;--radius-card:1rem;" + FONT_TOKENS["serif"] + "}"
    assert theme_css(ThemeValues("A", custom_css="  ")) == ""
    assert theme_css(ThemeValues("A", custom_css=".a{b:c}")) == ".a{b:c}"


def test_every_builtin_theme_passes_the_colour_rule():
    for slug, values in BUILTIN_THEMES.items():
        validate_brand_color(values.brand_color)
        assert values.custom_css == "", slug


def test_the_compiled_css_still_reads_the_brand_from_the_scale():
    css = SITE_CSS.read_text(encoding="utf-8")
    assert "--color-brand:var(--color-cobalt-600)" in css.replace(" ", "")


# --------------------------------------------------------------------------- fonts of a package (#636)


def test_fonts_become_font_faces_and_the_family_leads_the_stack():
    from skeleton.appearance.themes import ThemeFont, ThemeValues, theme_css_tokens

    font = ThemeFont("Aurora Sans", "100 900", "normal", "/brand/fonts/aurora.woff2?v=0123456789ab")
    css = theme_css_tokens(ThemeValues("T", font_family="Aurora Sans", font="serif", fonts=(font,)))
    assert css.startswith('@font-face{font-family:"Aurora Sans";src:url("/brand/fonts/aurora.woff2?v=0123456789ab")')
    assert "font-weight:100 900;font-style:normal;font-display:swap}" in css
    assert ':root{--font-sans:"Aurora Sans","Inter Variable",' in css and "ui-serif" not in css  # The family wins.


def test_invalid_fonts_are_never_written(subtests):
    from skeleton.appearance.themes import ThemeFont, ThemeValues, theme_css_tokens

    good = "/brand/fonts/a.woff2?v=0123456789ab"
    cases = [
        ThemeFont('A"};body{x:y', "400", "normal", good),
        ThemeFont("Aurora", "400", "normal", "https://evil.example/a.woff2"),
        ThemeFont("Aurora", "400", "normal", '/a.woff2")}body{'),
        ThemeFont("Aurora", "450", "normal", good),
        ThemeFont("Aurora", "400", "oblique", good),
    ]
    for font in cases:
        with subtests.test(font=font):
            css = theme_css_tokens(ThemeValues("T", font_family=font.family, fonts=(font,)))
            assert "@font-face" not in css and "--font-sans" not in css


def test_without_fonts_the_css_is_the_same_as_before():
    from skeleton.appearance.themes import BUILTIN_THEMES, ThemeValues, theme_css

    for values in (*BUILTIN_THEMES.values(), ThemeValues("Mar", "#0f766e", "round", "system", ".a{}")):
        assert "@font-face" not in theme_css(values)
    assert theme_css(ThemeValues("X", font_family="Ninguém")) == ""  # A family without its font is ignored.
