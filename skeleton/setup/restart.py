"""Ask the supervisor to restart the process in normal mode once the setup is complete.

- ``SKELETON_SETUP_RESTART=exit`` (containers): the process exits shortly after the response
  and the restart policy (``restart: unless-stopped``) starts it again in normal mode.
- Under Gunicorn: ``SIGHUP`` to the master reloads the workers with the new configuration.
  With ``gunicorn --preload`` the master already imported ``config.wsgi`` and the new workers
  inherit the setup settings: restart the whole service instead (#290).
- Under ``runserver`` with the autoreloader: touching this module triggers a reload.
Otherwise the page tells the operator to restart the service.
"""

from __future__ import annotations

import os
import signal
import threading
from pathlib import Path

# Gunicorn's arbiter halts the whole server when a worker exits with this code
# (Arbiter.WORKER_BOOT_ERROR); any other code would only respawn that one worker and leave
# the others in setup mode. Outside Gunicorn the process is the server, so any code ends it.
GUNICORN_HALT_EXIT_CODE = 3


def _delayed(action, seconds: float = 1.0) -> None:
    timer = threading.Timer(seconds, action)
    timer.daemon = True
    timer.start()


def request_restart() -> str:
    """Schedule the restart and return how (``exit``, ``gunicorn``, ``autoreload`` or ``manual``)."""
    if os.environ.get("SKELETON_SETUP_RESTART", "").lower() == "exit":
        _delayed(lambda: os._exit(GUNICORN_HALT_EXIT_CODE))
        return "exit"
    if "gunicorn" in os.environ.get("SERVER_SOFTWARE", "").lower() and hasattr(signal, "SIGHUP"):
        parent = os.getppid()
        _delayed(lambda: os.kill(parent, signal.SIGHUP))
        return "gunicorn"
    if os.environ.get("RUN_MAIN") == "true":  # Django's autoreloader child process.
        _delayed(lambda: Path(__file__).touch())
        return "autoreload"
    return "manual"
