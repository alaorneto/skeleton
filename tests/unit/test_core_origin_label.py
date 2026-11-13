"""The core origin on /platform/ is a translated label, never the raw code (#364)."""

import pytest
from django.utils import translation

from skeleton.web.platform import updates


@pytest.mark.parametrize(
    ("origin", "label"),
    [("installed", "pacote instalado"), ("source", "código-fonte"), ("unknown", "desconhecida")],
)
def test_every_core_origin_has_a_label(monkeypatch, origin, label):
    monkeypatch.setattr("skeleton.versioning.core_version", lambda: {"version": "1.2.3", "origin": origin})
    with translation.override("pt-br"):
        core = updates.running_core()
    assert core == {"version": "1.2.3", "origin": origin, "origin_label": label}


def test_labels_cover_every_origin_core_version_returns():
    import inspect

    from skeleton import versioning

    returned = set(__import__("re").findall(r'"origin": "([a-z]+)"', inspect.getsource(versioning.core_version)))
    assert returned == set(updates.CORE_ORIGIN_LABELS)
