"""Build-time static collection: no production credentials or DB access."""

from skeleton.conf.profiles import build_settings

from .base import *

globals().update(build_settings(globals()))
