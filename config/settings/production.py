from .base import *
from skeleton.conf.profiles import production_settings
globals().update(production_settings(globals()))
