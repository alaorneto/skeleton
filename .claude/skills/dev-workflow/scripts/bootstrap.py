#!/usr/bin/env python3
"""Instalação e conferência do fluxo num projeto (veja devflow/bootstrap.py)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from devflow.bootstrap import main

if __name__ == "__main__":
    raise SystemExit(main())
