"""[GATE2 tooling] Project-level conftest.

Makes the bare-package `taskq` importable for pytest, and allows the
gate2 integration suite to opt-in to subprocess coverage via the
``--gate2-cov`` flag. Both flags default to "off" so unit tests have
zero behaviour change.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent
_SRC_DIR = _REPO_ROOT / "03-development" / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))
