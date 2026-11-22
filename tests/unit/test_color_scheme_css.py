"""A theme stylesheet resolved for the forced colour scheme (#665): pure function, no database."""

from pathlib import Path

import pytest

from skeleton.appearance.color_scheme import ALWAYS, NEVER, force_color_scheme
from skeleton.appearance.css_validation import css_problem

DARK = "@media (prefers-color-scheme: dark){:root{--x:#000}}"
CADERNO = Path(__file__).resolve().parents[2] / "examples" / "themes" / "caderno" / "theme.css"


@pytest.mark.parametrize(
    ("css", "scheme", "expected"),
    [
        (DARK, "dark", f"@media {ALWAYS}{{:root{{--x:#000}}}}"),
        (DARK, "light", f"@media {NEVER}{{:root{{--x:#000}}}}"),
        ("@media (prefers-color-scheme: light){a{b:c}}", "light", f"@media {ALWAYS}{{a{{b:c}}}}"),
        ("@media (prefers-color-scheme: light){a{b:c}}", "dark", f"@media {NEVER}{{a{{b:c}}}}"),
        ("@MEDIA ( Prefers-Color-Scheme :  DARK ){a{b:c}}", "dark", f"@MEDIA {ALWAYS}{{a{{b:c}}}}"),
        ("@media screen and (prefers-color-scheme: dark){a{b:c}}", "light", f"@media screen and {NEVER}{{a{{b:c}}}}"),
        ("@media not (prefers-color-scheme: dark){a{b:c}}", "light", f"@media not {NEVER}{{a{{b:c}}}}"),
        (
            "@media print, (prefers-color-scheme: dark){a{b:c}}",
            "dark",
            f"@media print, {ALWAYS}{{a{{b:c}}}}",
        ),
        (
            "@supports (color: red){@media (prefers-color-scheme: dark){a{b:c}}}",
            "dark",
            f"@supports (color: red){{@media {ALWAYS}{{a{{b:c}}}}}}",
        ),
        (
            "@layer theme{@media (prefers-color-scheme: dark){a{b:c}}}",
            "light",
            f"@layer theme{{@media {NEVER}{{a{{b:c}}}}}}",
        ),
    ],
)
def test_the_feature_becomes_a_fixed_literal(css, scheme, expected):
    assert force_color_scheme(css, scheme) == expected


def test_comments_strings_and_other_rules_are_never_touched(subtests):
    cases = {
        "comment": "/* @media (prefers-color-scheme: dark) */a{b:c}",
        "string": 'a::after{content:"@media (prefers-color-scheme: dark){"}',
        "no media": "a{b:c}",
        "supports only": "@supports (prefers-color-scheme: dark){a{b:c}}",
        "bare feature": "@media (prefers-color-scheme){a{b:c}}",
        "escaped at-rule": "@\\6d edia (prefers-color-scheme: dark){a{b:c}}",
    }
    for name, css in cases.items():
        with subtests.test(case=name):
            assert force_color_scheme(css, "light") == css
            assert force_color_scheme(css, "dark") == css


def test_a_comment_inside_the_prelude_is_kept_and_the_feature_still_fixed():
    css = "@media /* (prefers-color-scheme: light) */ (prefers-color-scheme: dark){a{b:c}}"
    assert force_color_scheme(css, "dark") == f"@media /* (prefers-color-scheme: light) */ {ALWAYS}{{a{{b:c}}}}"


def test_the_output_still_passes_the_css_barrier(subtests):
    from skeleton.appearance.themes import ThemeValues, theme_css

    sources = {
        "caderno": CADERNO.read_text(encoding="utf-8"),
        "generated tokens": theme_css(ThemeValues("Teste", brand_color="#0f766e", custom_css=DARK)),
    }
    for name, css in sources.items():
        assert css_problem(css) is None
        for scheme in ("light", "dark"):
            with subtests.test(source=name, scheme=scheme):
                forced = force_color_scheme(css, scheme)
                assert css_problem(forced) is None
                assert "prefers-color-scheme" not in forced


def test_an_unknown_scheme_is_refused():
    with pytest.raises(ValueError):
        force_color_scheme(DARK, "sepia")
