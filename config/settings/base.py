"""Project-owned overlay; reusable core defaults live in the installed wheel."""

from skeleton.conf.base import *
from skeleton.conf.bundled import project_library_configs

from .project import PROJECT_APPS

# A first-party library a project app depends on is installed with it (#793).
INSTALLED_APPS = [*INSTALLED_APPS, *project_library_configs(PROJECT_APPS, installed=INSTALLED_APPS), *PROJECT_APPS]
SKELETON_ENABLE_EXAMPLE_APP = env.bool("SKELETON_ENABLE_EXAMPLE_APP", default=False)
if SKELETON_ENABLE_EXAMPLE_APP:
    INSTALLED_APPS.append("examples.notes.apps.NotesConfig")
