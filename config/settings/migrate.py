from skeleton.conf.profiles import migrate_settings

from .base import *

globals().update(migrate_settings(globals()))
