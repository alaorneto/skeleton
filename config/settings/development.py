from .base import *
from skeleton.conf.profiles import development_settings
globals().update(development_settings(globals()))
