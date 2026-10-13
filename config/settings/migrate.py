from .base import *
from skeleton.conf.profiles import migrate_settings
globals().update(migrate_settings(globals()))
