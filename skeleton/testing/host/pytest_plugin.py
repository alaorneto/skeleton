"""pytest plugin of the host project (#520): test modules of a dev app import under ``applications.<package>``.

pytest's default import mode would import ``<checkout>/tests/test_x.py`` as ``<package>.tests.test_x``,
a second copy of the app beside the ``applications.<package>`` that Django loaded, and the app's
models would be registered twice. The generated ``pyproject.toml`` enables this plugin
(``-p skeleton.testing.host.pytest_plugin``); it only affects files inside a ``SKELETON_DEV_APPS``
checkout.
"""

import importlib
from pathlib import Path
from types import ModuleType

import pytest

from skeleton.conf.dev_apps import managed_module_name


class ManagedModule(pytest.Module):
    managed_name = ""

    def _getobj(self) -> ModuleType:
        return importlib.import_module(self.managed_name)


def pytest_pycollect_makemodule(module_path: Path, parent: pytest.Collector) -> pytest.Module | None:
    name = managed_module_name(module_path)
    if name is None:
        return None
    module = ManagedModule.from_parent(parent, path=module_path)
    module.managed_name = name
    return module
