"""The stroke icon an app ships for its menu entries (#842): accepted shape and refusals."""

import json

import pytest

from skeleton.common.stroke_icons import MAX_BYTES, StrokeIconError, stroke_icon_markup
from skeleton.system.packages import PackageError, canonical_json, manifest_template, validate_manifest

# The shape of an outline icon as design tools export it: presentation on the root and paths.
EXPORTED = (
    '<?xml version="1.0" encoding="UTF-8"?>\n<!-- exported -->\n'
    '<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="1.5" '
    'stroke="currentColor" class="size-6"><path stroke-linecap="round" stroke-linejoin="round" '
    'd="M12 4.5v15m7.5-7.5h-15"/>\n  <circle cx="12" cy="12" r="3"/></svg>'
)


def svg(inner, root='xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"'):
    return f"<svg {root}>{inner}</svg>".encode()


def test_an_exported_outline_icon_is_rebuilt_as_geometry_only():
    assert stroke_icon_markup(EXPORTED.encode()) == '<path d="M12 4.5v15m7.5-7.5h-15"/><circle cx="12" cy="12" r="3"/>'
    assert stroke_icon_markup(svg('<rect height="4" width="6" x="1"/>')) == '<rect x="1" width="6" height="4"/>'


def test_refusals(subtests):
    cases = [
        ("script", svg("<script>alert(1)</script>"), "element"),
        ("handler", svg('<path d="M1 1" onclick="x()"/>'), "attribute"),
        ("fill", svg('<path d="M1 1" fill="red"/>'), "attribute"),
        ("style", svg('<path d="M1 1" style="stroke:red"/>'), "attribute"),
        ("reference", svg('<use href="#a"/>'), "element"),
        ("group", svg('<g><path d="M1 1"/></g>'), "element"),
        ("text", svg("<text>A</text>"), "element"),
        ("foreign", svg("<foreignObject/>"), "element"),
        ("value", svg('<path d="url(#x)"/>'), "value"),
        ("missing geometry", svg("<circle/>"), "attribute"),
        ("other box", svg('<path d="M1 1"/>', 'xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20"'), "view_box"),
        ("no box", svg('<path d="M1 1"/>', 'xmlns="http://www.w3.org/2000/svg"'), "view_box"),
        (
            "root fill",
            svg('<path d="M1 1"/>', 'xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="red"'),
            "value",
        ),
        ("no namespace", b'<svg viewBox="0 0 24 24"><path d="M1 1"/></svg>', "not_svg"),
        ("empty", svg(""), "empty"),
        ("doctype", b'<!DOCTYPE svg [<!ENTITY a "x">]>' + svg('<path d="M1 1"/>'), "doctype_or_pi"),
        ("instruction", b'<?xml-stylesheet href="a.css"?>' + svg('<path d="M1 1"/>'), "doctype_or_pi"),
        ("malformed", b"<svg", "malformed"),
        ("encoding", b"\xff\xfe<svg/>", "encoding"),
        ("too large", svg('<path d="M1 1"/>' + " " * MAX_BYTES), "too_large"),
        ("too complex", svg('<path d="M1 1"/>' * 33), "too_complex"),
    ]
    for case, content, code in cases:
        with subtests.test(case=case):
            with pytest.raises(StrokeIconError) as raised:
                stroke_icon_markup(content)
            assert raised.value.code == code


@pytest.fixture
def manifest():
    value = manifest_template(
        app_id="org.test.demo",
        app_label="project_demo",
        package_name="package_demo",
        publisher="test-publisher",
        app_config_class="DemoConfig",
        service_classes=["DemoService"],
    )
    value["compatibility"]["skeleton"] = ">=0.10.0,<1.0.0"
    value["runtime"]["navigation"] = [{"label": "Demo", "url_name": "demo:home", "table_key": "project_demo.item"}]
    return json.loads(canonical_json(value))


def with_icon(manifest, **icon):
    value = json.loads(canonical_json(manifest))
    value["runtime"]["navigation"][0].update(icon)
    return value


def test_a_navigation_entry_declares_its_icon(manifest):
    assert validate_manifest(with_icon(manifest, icon="calendar"))["runtime"]["navigation"][0]["icon"] == "calendar"
    assert validate_manifest(with_icon(manifest, icon_svg="icons/demo.svg"))["runtime"]["navigation"][0]["icon_svg"]
    assert "icon" not in validate_manifest(manifest)["runtime"]["navigation"][0]  # Optional: the default stays.


def test_navigation_icon_refusals(manifest, subtests):
    cases = [
        ("both", {"icon": "calendar", "icon_svg": "icons/a.svg"}, "not both"),
        ("icon name", {"icon": "Calendar"}, "name a concept"),
        ("icon type", {"icon": 3}, "name a concept"),
        ("not svg", {"icon_svg": "icons/a.png"}, "must be an .svg"),
        ("role directory", {"icon_svg": "templates/a.svg"}, "must be an .svg"),
        ("unsafe path", {"icon_svg": "../a.svg"}, "Unsafe"),
        ("absolute", {"icon_svg": "/tmp/a.svg"}, "Unsafe"),
    ]
    for case, icon, message in cases:
        with subtests.test(case=case):
            with pytest.raises(PackageError, match=message):
                validate_manifest(with_icon(manifest, **icon))
    with subtests.test(case="floor"):
        low = with_icon(manifest, icon="calendar")
        low["compatibility"]["skeleton"] = ">=0.9.0,<1.0.0"
        with pytest.raises(PackageError, match="navigation icon requires a core minimum of 0.10.0"):
            validate_manifest(low)
    with subtests.test(case="manifest v1"):
        old = with_icon(manifest, icon="calendar")
        old["manifest_version"] = 1
        with pytest.raises(PackageError):
            validate_manifest(old)
