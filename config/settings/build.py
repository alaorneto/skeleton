"""Build-time static collection: no production credentials or DB access."""
from .base import *
from skeleton.conf.profiles import build_settings
globals().update(build_settings(globals()))
