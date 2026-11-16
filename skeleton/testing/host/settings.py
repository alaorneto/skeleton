"""Test settings of the host project: the core defaults plus ``test_settings``, without ``config``.

The project root defaults to the current directory (``.env`` and ``var/`` are read from there);
``SKELETON_PROJECT_ROOT`` overrides it. Every ``config.*`` name the core settings reference is
replaced here, so the ``config`` package is never imported.
"""

import os

os.environ.setdefault("SKELETON_PROJECT_ROOT", os.getcwd())

from skeleton.conf.base import *  # noqa: E402,F403
from skeleton.conf.profiles import test_settings  # noqa: E402

globals().update(test_settings(globals()))
ROOT_URLCONF = "skeleton.testing.host.urls"
WSGI_APPLICATION = "skeleton.testing.host.wsgi.application"
ASGI_APPLICATION = "skeleton.testing.host.asgi.application"
