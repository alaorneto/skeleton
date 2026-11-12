#!/usr/bin/env python3
"""Versionamento semântico automatizado (veja devflow/release.py)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from devflow.release import main

if __name__ == "__main__":
    raise SystemExit(main())
