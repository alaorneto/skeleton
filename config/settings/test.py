from skeleton.conf.profiles import test_settings

from .base import *

globals().update(test_settings(globals()))
