"""[FR-01] taskq — minimal task-queue CLI.

Citations:
  03-development/tests/test_fr01.py:35 — `from taskq import cli` exercises
    this package's public surface.
  03-development/tests/test_fr02.py:41 — `from taskq import cli, executor`
    exercises both the CLI surface and the executor entry points.
"""

__all__ = ["cli", "executor", "store"]
__version__ = "0.1.0"