from skeleton.conf.profiles import development_settings

from .base import *

globals().update(development_settings(globals()))
