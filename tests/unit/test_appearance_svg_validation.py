"""The barrier of SVG logos of theme packages (#632): a static subset is accepted, the rest refused."""

import os
import subprocess
import sys

from skeleton.appearance.svg_validation import MAX_BYTES, SvgProblem, svg_problem

NS = 'xmlns="http://www.w3.org/2000/svg"'
XL = 'xmlns:xlink="http://www.w3.org/1999/xlink"'

OPTIMIZED = f"""<?xml version="1.0" encoding="UTF-8"?>
<svg {NS} viewBox="0 0 120 40" width="120" height="40">
  <title>Aurora</title>
  <defs>
    <linearGradient id="g1" x1="0" x2="1"><stop offset="0" stop-color="#1273c4"/><stop offset="1" stop-color="#2bb673"/></linearGradient>
    <symbol id="mark"><circle cx="20" cy="20" r="18"/></symbol>
  </defs>
  <!-- comentário de editor -->
  <g fill="url(#g1)" style="fill-opacity:.9;stroke:url(#g1)">
    <use href="#mark"/>
    <path d="M40 10h70v20H40z" clip-path="url( #g1 )"/>
    <text x="45" y="26" font-family="Inter">Aurora<tspan dx="2">.</tspan> texto</text>
  </g>
</svg>""".encode()


def svg(body: str, root: str = f'<svg {NS} viewBox="0 0 10 10">') -> bytes:
    return f"{root}{body}</svg>".encode()


REFUSED = [
    ("script", svg("<script>alert(1)</script>"), SvgProblem("element", "script")),
    ("onload", svg("", f'<svg {NS} viewBox="0 0 1 1" onload="alert(1)">'), SvgProblem("attribute", "onload")),
    ("ONCLICK", svg('<rect ONCLICK="x"/>'), SvgProblem("attribute", "ONCLICK")),
    ("foreignObject", svg("<foreignObject><p>x</p></foreignObject>"), SvgProblem("element", "foreignObject")),
    ("image", svg('<image href="https://x/y.png"/>'), SvgProblem("element", "image")),
    ("external use", svg('<use href="https://x/y.svg#a"/>'), SvgProblem("reference", "href")),
    (
        "xlink javascript",
        svg('<use xlink:href="javascript:alert(1)"/>', f'<svg {NS} {XL} viewBox="0 0 1 1">'),
        SvgProblem("reference", "href"),
    ),
    ("href on rect", svg('<rect href="#a"/>'), SvgProblem("reference", "href")),
    ("style url", svg('<rect style="fill:url(https://x)"/>'), SvgProblem("style", "style")),
    ("style import", svg('<rect style="@import x"/>'), SvgProblem("style", "style")),
    ("fill url", svg('<rect fill="url(https://x/a)"/>'), SvgProblem("reference", "fill")),
    ("data uri", svg('<rect fill="data:image/png;base64,AAAA"/>'), SvgProblem("reference", "fill")),
    ("style element", svg("<style>rect{fill:red}</style>"), SvgProblem("element", "style")),
    ("animate", svg('<animate attributeName="href" to="javascript:x"/>'), SvgProblem("element", "animate")),
    ("set", svg('<set attributeName="href"/>'), SvgProblem("element", "set")),
    ("a", svg('<a href="#x"><rect/></a>'), SvgProblem("element", "a")),
    (
        "doctype entity",
        b'<!DOCTYPE svg [<!ENTITY a "aaaa">]>' + svg("<text>&a;</text>"),
        SvgProblem("doctype_or_pi"),
    ),
    ("stylesheet pi", b'<?xml-stylesheet href="x.css"?>' + svg(""), SvgProblem("doctype_or_pi")),
    ("cdata", svg("<text><![CDATA[x]]></text>"), SvgProblem("doctype_or_pi")),
    ("html root", b"<html><body/></html>", SvgProblem("not_svg", "html")),
    ("svg without namespace", b'<svg viewBox="0 0 1 1"/>', SvgProblem("not_svg", "svg")),
    ("no size", f"<svg {NS}/>".encode(), SvgProblem("not_svg", "viewBox")),
    (
        "editor namespace",
        svg("", f'<svg {NS} xmlns:i="urn:x" viewBox="0 0 1 1" i:x="1">'),
        SvgProblem("attribute", "x"),
    ),
    (
        "editor element",
        svg("<i:meta/>", f'<svg {NS} xmlns:i="urn:x" viewBox="0 0 1 1">'),
        SvgProblem("element", "meta"),
    ),
    ("text in g", svg("<g>texto solto</g>"), SvgProblem("text", "g")),
    ("not utf-8", svg("<title>\xe7</title>").decode().encode("latin-1"), SvgProblem("encoding")),
    ("malformed", b"<svg", SvgProblem("malformed")),
    ("too large", b" " * (MAX_BYTES + 1), SvgProblem("too_large")),
    ("not bytes", "texto", SvgProblem("too_large")),
    ("too deep", svg("<g>" * 32 + "</g>" * 32), SvgProblem("too_complex")),
]


def test_optimized_svg_is_accepted():
    assert svg_problem(OPTIMIZED) is None
    assert svg_problem(b"\xef\xbb\xbf" + OPTIMIZED) is None  # UTF-8 with BOM.
    assert svg_problem(svg("<g>" * 31 + "</g>" * 31)) is None  # 32 levels with the root.
    assert svg_problem(f'<svg {NS} width="1" height="1"/>'.encode()) is None


def test_refused(subtests):
    for case, content, expected in REFUSED:
        with subtests.test(case=case):
            assert svg_problem(content) == expected


def test_the_problem_never_carries_a_value():
    problem = svg_problem(svg('<rect fill="url(https://attacker.example/x)"/>'))
    assert problem is not None and "attacker" not in repr(problem)


def test_the_check_does_not_import_django():
    environment = {key: value for key, value in os.environ.items() if key != "DJANGO_SETTINGS_MODULE"}
    code = (
        "import sys, skeleton.appearance.svg_validation as s; s.svg_problem(b'<svg/>'); print('django' in sys.modules)"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=environment, check=True)
    assert result.stdout.strip() == "False"
