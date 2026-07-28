"""[SAB persistence layer] ``taskq.models`` — shared data shape + state constants.

SAD §2.2 declares a persistence-layer companion to ``taskq.store`` for the
``Task`` record dataclass and the state constants ``PENDING`` /
``RUNNING`` / ``DONE`` / ``FAILED`` / ``TIMEOUT``. The current store uses
plain ``dict`` records (see ``taskq.store.load_store``), so this module
ships empty today and is reserved for the next refactor that lifts the
record schema into a typed dataclass. Constants live here so callers can
``from taskq.models import PENDING`` instead of duplicating string literals
across modules.
"""