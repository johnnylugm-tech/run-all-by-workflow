"""[FR-01] Module entry point for ``python -m taskq``.

Citations:
  03-development/tests/test_fr01.py:75 — ``python -m taskq ...`` subprocess
    invocation; sys.argv is populated by the interpreter and forwarded to
    ``cli.main``.
"""

from __future__ import annotations

import sys

from taskq.cli import main

if __name__ == "__main__":
    sys.exit(main())
