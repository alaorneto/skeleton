"""The barrier of the free CSS of a theme (#594): accepted, refused and the original text kept."""

import random

import pytest
from django.core.exceptions import ValidationError

from skeleton.appearance.css_validation import FORBIDDEN, MAX_BYTES, normalized, validate_custom_css

ACCEPTED = [
    ":root{--color-canvas:#fff}",
    ".btn-primary{border-radius:9999px}",
    "@media (min-width: 640px){.card{padding:2rem}}",
    ".sm\\:px-6{padding-inline:1.5rem}",
    'a::after{content:"a}b"}',
    "/* comentário */ .x{color:red}",
    ".x{background:linear-gradient(red,blue)}",
]
REFUSED = [
    "@import 'x.css';",
    "@IMPORT 'x.css';",
    "@charset 'utf-8';",
    "@namespace svg;",
    ".a{background:u\\72l(x)}",
    ".a{background:\\75 rl(x)}",
    "@im/**/port 'x';",
    ".a{background:URL(//x)}",
    ".a{background:image-set('x' 1x)}",
    ".a{background:image('x')}",
    ".a{src:src('x')}",
    ".a{width:expression(alert(1))}",
    ".a{background:javascript:alert(1)}",
    ".a{behavior:x}",
    ".a{-moz-binding:x}",
    '.a{content:"/*"} @import "x"; .b{content:"*/"}',
]
MALFORMED = [
    "</style><script>",
    ".a{b:c}\x00",
    '.a{content:"sem fim}',
    "/* sem fim",
    ".a{b:c}}",
    ".a{b:c",
    ".a{width:calc(1px}",
]


def test_accepted(subtests):
    for text in ACCEPTED:
        with subtests.test(text=text):
            assert validate_custom_css(text) == text


def test_refused_items_even_disguised(subtests):
    for text in REFUSED:
        with subtests.test(text=text):
            with pytest.raises(ValidationError) as error:
                validate_custom_css(text)
            assert "custom_css" in error.value.message_dict


def test_refused_malformed(subtests):
    for text in MALFORMED:
        with subtests.test(text=repr(text)):
            with pytest.raises(ValidationError) as error:
                validate_custom_css(text)
            assert "custom_css" in error.value.message_dict


def test_size_limit_counts_bytes():
    assert validate_custom_css("a" * MAX_BYTES) == "a" * MAX_BYTES
    with pytest.raises(ValidationError):
        validate_custom_css("a" * (MAX_BYTES + 1))
    with pytest.raises(ValidationError):
        validate_custom_css("é" * (MAX_BYTES // 2 + 1))  # Fewer characters than the limit, more bytes.


def test_type_empty_and_original_text():
    with pytest.raises(ValidationError):
        validate_custom_css(None)
    assert validate_custom_css("   \n ") == ""
    text = ".A{Color:RED}\r\n/* x */"
    assert validate_custom_css(text) == ".A{Color:RED}\n/* x */"


def test_the_message_names_the_forbidden_item():
    with pytest.raises(ValidationError) as error:
        validate_custom_css(".a{background:url(x)}")
    assert "url(" in error.value.message_dict["custom_css"][0]


def test_random_text_never_passes_with_a_forbidden_item():
    alphabet = list("url(){}/*\\72 5@import;:\"'abegimnopstxv-")
    generator = random.Random(591)
    for _ in range(3000):
        text = "".join(generator.choice(alphabet) for _ in range(generator.randint(1, 40)))
        try:
            accepted = validate_custom_css(text)
        except ValidationError:
            continue
        form = normalized(accepted)
        assert not any(item in form for item in FORBIDDEN), text
