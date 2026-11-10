"""Ask the supervisor to restart the process in normal mode once the setup is complete.

- ``SKELETON_SETUP_RESTART=exit`` (containers): the process exits shortly after the response
  and the restart policy (``restart: unless-stopped``) starts it again in normal mode.
- Under Gunicorn: ``SIGHUP`` to the master reloads the workers with the new configuration.
- Under ``runserver`` with the autoreloader: touching this module triggers a reload.
Otherwise the page tells the operator to restart the service.
"""

from __future__ import annotations

import os
import signal
import threading
from pathlib import Path


def _delayed(action, seconds: float = 1.0) -> None:
    timer = threading.Timer(seconds, action)
    timer.daemon = True
    timer.start()


def request_restart() -> str:
    """Schedule the restart and return how (``exit``, ``gunicorn``, ``autoreload`` or ``manual``)."""
    if os.environ.get("SKELETON_SETUP_RESTART", "").lower() == "exit":
        _delayed(lambda: os._exit(3))
        return "exit"
    if "gunicorn" in os.environ.get("SERVER_SOFTWARE", "").lower() and hasattr(signal, "SIGHUP"):
        parent = os.getppid()
        _delayed(lambda: os.kill(parent, signal.SIGHUP))
        return "gunicorn"
    if os.environ.get("RUN_MAIN") == "true":  # Django's autoreloader child process.
        _delayed(lambda: Path(__file__).touch())
        return "autoreload"
    return "manual"
