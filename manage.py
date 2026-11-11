#!/usr/bin/env python
import os
import sys
from pathlib import Path

if __name__ == "__main__":
    default = "config.settings.development"
    if sys.argv[1:2] == ["runserver"]:
        # runserver of an unconfigured instance opens the first-access setup (#146).
        from skeleton.setup.mode import select_settings_module

        os.environ["DJANGO_SETTINGS_MODULE"] = select_settings_module(default, base_dir=Path(__file__).resolve().parent)
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", default)
    from django.core.management import execute_from_command_line

    execute_from_command_line(sys.argv)
