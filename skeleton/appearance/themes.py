"""CSS of an instance theme from its parameters (#593): pure Python, no database.

The chosen colour is step 600 of the brand scale and must reach WCAG AA (4.5:1) against white,
the same rule as the organization colour (``skeleton.tenancy.branding.validate_brand_color``).
The whole ``--color-cobalt-50…950`` scale comes from it by mixing with white and black in sRGB:
the compiled ``site.css`` keeps ``--color-brand: var(--color-cobalt-600)`` and the other brand
tokens as references to that scale, so swapping it swaps buttons, links, focus, soft backgrounds
and the login gradient at once. The dark tokens that ``tokens.css`` pins as ``oklch(...)`` are
emitted explicitly, lightened until they reach AA on the dark canvas.

The generated part holds only ``#rrggbb`` values and the fixed radius and font literals; the
free CSS of the theme (validated by ``css_validation``) is always appended last.

A theme package (#636) may bring fonts: each one becomes an ``@font-face`` with a URL the caller
builds (``/brand/fonts/<file>?v=<sha>``), and ``font_family`` puts that family in front of the
core stack of ``--font-sans``. Families and URLs are checked again here, because this is the
function that writes CSS; anything outside the pattern is left out, never written.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

LIGHT_STEPS = {50: 0.95, 100: 0.88, 200: 0.76, 300: 0.58, 400: 0.32, 500: 0.14}
DARK_STEPS = {700: 0.18, 800: 0.34, 900: 0.48, 950: 0.64}
RADIUS_TOKENS = {
    "sharp": "--radius-base:.125rem;--radius-card:.25rem",
    "round": "--radius-base:.75rem;--radius-card:1rem",
}
FONT_TOKENS = {
    "system": '--font-sans:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif',
    "serif": '--font-sans:ui-serif,Georgia,Cambria,"Times New Roman",Times,serif',
}
# The core stack of frontend/src/tokens.css; a package family goes in front of it.
CORE_FONT_STACK = (
    '"IBM Plex Sans Variable",ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,'
    '"Noto Sans",sans-serif,"Apple Color Emoji","Segoe UI Emoji"'
)
_FAMILY = re.compile(r"[A-Za-z][A-Za-z0-9 -]{0,62}[A-Za-z0-9]")
_FONT_URL = re.compile(r"/[A-Za-z0-9/_.-]+(\?v=[0-9a-f]{12})?")
_WEIGHT = re.compile(r"[1-9]00( [1-9]00)?")


@dataclass(frozen=True, slots=True)
class ThemeFont:
    """One font of a theme package; ``url`` is built by the caller (public route or preview)."""

    family: str
    weight: str
    style: str
    url: str


@dataclass(frozen=True, slots=True)
class ThemeValues:
    """The parameters of one theme; ``name`` is never written into the CSS."""

    name: str
    brand_color: str = ""
    radius: str = "default"
    font: str = "default"
    custom_css: str = ""
    font_family: str = ""
    fonts: tuple[ThemeFont, ...] = ()


BUILTIN_THEMES: dict[str, ThemeValues] = {
    "cobalto": ThemeValues("Cobalto"),
    "esmeralda": ThemeValues("Esmeralda", brand_color="#047857"),
    "grafite": ThemeValues("Grafite", brand_color="#374151"),
    "vinho": ThemeValues("Vinho", brand_color="#9f1239"),
    "violeta": ThemeValues("Violeta", brand_color="#6d28d9"),
}


def scale(color: str) -> dict[int, str]:
    """The ``--color-cobalt-*`` scale of a validated colour: step 600 is the colour itself."""
    from skeleton.tenancy.branding import BLACK, WHITE, _hex, _mix, _rgb

    rgb = _rgb(color)
    steps = {step: _hex(_mix(rgb, WHITE, amount)) for step, amount in LIGHT_STEPS.items()}
    steps[600] = color
    steps.update({step: _hex(_mix(rgb, BLACK, amount)) for step, amount in DARK_STEPS.items()})
    return dict(sorted(steps.items()))


def dark_tokens(color: str) -> dict[str, str]:
    """The dark-mode brand tokens, with the text colours lightened to AA on the dark backgrounds."""
    from skeleton.tenancy.branding import AA, DARK_CANVAS, WHITE, _hex, _mix, _rgb, contrast

    rgb = _rgb(color)
    softer = _mix(rgb, DARK_CANVAS, 0.82)
    step = next(step for step in range(0, 21) if contrast(_mix(rgb, WHITE, step / 20), DARK_CANVAS) >= AA)
    while step < 20 and contrast(_mix(rgb, WHITE, step / 20), softer) < AA:
        step += 1  # Lighten in 5 % steps until the text also reads on the softest background.
    fg = _mix(rgb, WHITE, step / 20)
    fg_strong = _mix(_rgb(_hex(fg)), WHITE, 0.3)
    return {
        "--color-brand-softer": _hex(softer),
        "--color-brand-soft": _hex(_mix(rgb, DARK_CANVAS, 0.72)),
        "--color-brand-medium": _hex(_mix(rgb, DARK_CANVAS, 0.55)),
        "--color-brand-subtle": _hex(_mix(rgb, DARK_CANVAS, 0.62)),
        "--color-fg-brand": _hex(fg),
        "--color-fg-brand-strong": _hex(fg_strong),
        "--color-focus": scale(color)[400],
    }


def _font_faces(fonts: tuple[ThemeFont, ...]) -> tuple[str, set[str]]:
    """The ``@font-face`` rules of the valid fonts and the families they declare."""
    rules, families = [], set()
    for font in fonts:
        if (
            not _FAMILY.fullmatch(font.family)
            or not _FONT_URL.fullmatch(font.url)
            or not _WEIGHT.fullmatch(font.weight)
            or font.style not in ("normal", "italic")
        ):
            continue
        rules.append(
            f'@font-face{{font-family:"{font.family}";src:url("{font.url}") format("woff2");'
            f"font-weight:{font.weight};font-style:{font.style};font-display:swap}}"
        )
        families.add(font.family)
    return "".join(rules), families


def theme_css_tokens(values: ThemeValues) -> str:
    """The generated part only (tokens and fonts), without the free CSS; ``""`` for the core theme."""
    from skeleton.tenancy.branding import HEX

    light: list[str] = []
    dark = ""
    color = (values.brand_color or "").lower()
    if color and HEX.fullmatch(color):
        light += [f"--color-cobalt-{step}:{value}" for step, value in scale(color).items()]
        dark = ";".join(f"{name}:{value}" for name, value in dark_tokens(color).items())
    if values.radius in RADIUS_TOKENS:
        light.append(RADIUS_TOKENS[values.radius])
    faces, families = _font_faces(values.fonts)
    if values.font_family and values.font_family in families:
        light.append(f'--font-sans:"{values.font_family}",{CORE_FONT_STACK}')
    elif values.font in FONT_TOKENS:
        light.append(FONT_TOKENS[values.font])
    css = faces + (f":root{{{';'.join(light)}}}" if light else "")
    if dark:
        css += f"@media (prefers-color-scheme: dark){{:root{{{dark}}}}}"
    return css


def theme_css(values: ThemeValues) -> str:
    """Tokens, then the dark block, then the free CSS last; deterministic, ``""`` for the core theme."""
    tokens = theme_css_tokens(values)
    custom = values.custom_css or ""
    if not custom.strip():
        return tokens
    return f"{tokens}\n{custom}" if tokens else custom


def theme_digest(css: str) -> str:
    """First 12 hex digits of the SHA-256 of the CSS (the ``?v=`` of its URL); ``""`` for no CSS."""
    return hashlib.sha256(css.encode("utf-8")).hexdigest()[:12] if css else ""
