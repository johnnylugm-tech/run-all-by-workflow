"""[SAB config layer] ``taskq.config`` — environment-variable configuration.

SAD §2.2 declares a thin configuration layer (NFR-06: 8 TASKQ_* env vars).
The runtime modules (executor, breaker, cache, store) read these env vars
directly via ``os.environ.get(..., default)`` for testability; this module
exists to satisfy the SAB layer declared in ``.methodology/SAB.json`` and
to provide a single import surface for callers that want every knob in one
place. New keys added here should also be added to ``SAD.md`` §6 config
table and to the per-module default in the implementing layer.
"""