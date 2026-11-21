#!/usr/bin/env python3
"""Instalação e conferência do fluxo num projeto (veja devflow/bootstrap.py)."""

import sys
from pathlib import Path

if sys.version_info < (3, 11):  # noqa: UP036 (tomllib: sem isto o erro seria um ImportError sem explicação)
    sys.exit(f"ERRO: o dev-workflow precisa de Python 3.11 ou mais novo (este é {sys.version.split()[0]}).")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from devflow.bootstrap import main

if __name__ == "__main__":
    raise SystemExit(main())
