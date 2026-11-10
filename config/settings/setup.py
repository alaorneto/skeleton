"""First-access setup: no database until the setup configures it (#146)."""

from skeleton.conf.profiles import setup_settings

from .base import *

globals().update(setup_settings(globals()))
