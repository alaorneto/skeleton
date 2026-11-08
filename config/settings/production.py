from skeleton.conf.profiles import production_settings

from .base import *

globals().update(production_settings(globals()))
