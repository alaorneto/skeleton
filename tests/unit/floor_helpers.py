"""Manifest core floors above 0.0.0, to prove the floor mechanism (#569).

Every published core (0.1.0 on) understands the manifest fields that exist today, so their floors
are 0.0.0 and no manifest can fall below them. The mechanism still guards the fields of future
releases: these helpers put representative floors back (the numbers of the development history)
in every module that reads them, for the duration of one test.
"""

import pytest

FLOORS = {
    "MANIFEST_V2_CORE_MINIMUM": "0.2.0",
    "FIELD_DIMENSIONS_CORE_MINIMUM": "0.3.0",
    "SERVICE_API_CORE_MINIMUM": "0.4.0",
    "PORTAL_CORE_MINIMUM": "0.6.0",
}
MODULES = (
    "skeleton.system.packaging.limits",
    "skeleton.system.packaging.manifest",
    "skeleton.system.packaging.template",
    "skeleton.system.packaging.build",
    "skeleton.system.presentation.specs",
    "skeleton.system.presentation.runtime",
    "skeleton.system.presentation",
    "skeleton.system.packages",
)


@pytest.fixture
def raised_floors(monkeypatch):
    """The manifest floors of the development history (0.2.0, 0.3.0, 0.4.0 and 0.6.0)."""
    import importlib

    for name in MODULES:
        module = importlib.import_module(name)
        for constant, value in FLOORS.items():
            if hasattr(module, constant):
                monkeypatch.setattr(module, constant, value)
    return FLOORS
