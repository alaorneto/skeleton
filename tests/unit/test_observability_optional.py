"""Without configuration the observability extras are never imported (#175, #176, #177)."""

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EXTRA_MODULES = ("prometheus_client", "opentelemetry", "sentry_sdk")
SETTINGS = ("SKELETON_METRICS_TOKEN", "SKELETON_SENTRY_DSN", "SENTRY_DSN", "PROMETHEUS_MULTIPROC_DIR")

SCRIPT = """
import json, sys
import django
django.setup()
from django.core.management import call_command
from django.test import Client
call_command("check", verbosity=0)
statuses = [Client().get(path).status_code for path in ("/health/live/", "/metrics")]
loaded = sorted({name.split(".")[0] for name in sys.modules} & set(sys.argv[1:]))
print(json.dumps({"statuses": statuses, "loaded": loaded}))
"""


def test_unconfigured_process_imports_no_extra():
    environment = {
        name: value for name, value in os.environ.items() if name not in SETTINGS and not name.startswith("OTEL_")
    }
    environment["DJANGO_SETTINGS_MODULE"] = "config.settings.test"
    result = subprocess.run(
        [sys.executable, "-c", SCRIPT, *EXTRA_MODULES],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert report == {"statuses": [200, 404], "loaded": []}, result.stderr[-2000:]
