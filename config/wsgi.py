import os
from pathlib import Path

from django.core.wsgi import get_wsgi_application

from skeleton.setup.mode import select_settings_module

# Unconfigured instance (no secret key, hosts or database anywhere): start the first-access setup.
os.environ["DJANGO_SETTINGS_MODULE"] = select_settings_module(
    "config.settings.production", base_dir=Path(__file__).resolve().parents[1]
)
application = get_wsgi_application()
