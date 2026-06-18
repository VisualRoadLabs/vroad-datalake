"""Añade el directorio fuente de privacy a sys.path para importar `src.*`."""
from __future__ import annotations

import os
import sys

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
_JOB_DIR = os.path.join(_REPO_ROOT, "jobs", "privacy")
if _JOB_DIR not in sys.path:
    sys.path.insert(0, _JOB_DIR)
