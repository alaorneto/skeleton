"""Project-owned overlay; reusable core defaults live in the installed wheel."""
from skeleton.conf.base import *
from .project import PROJECT_APPS

INSTALLED_APPS = [*INSTALLED_APPS, *PROJECT_APPS]
SKELETON_ENABLE_EXAMPLE_APP = env.bool("SKELETON_ENABLE_EXAMPLE_APP", default=False)
if SKELETON_ENABLE_EXAMPLE_APP:
    INSTALLED_APPS.append("examples.notes.apps.NotesConfig")
