"""[GATE2] taskq integration tests — cross-process CLI flows.

Each test spawns the taskq CLI as a real subprocess (separate Python
interpreter, separate file-handle cache) so the on-disk tasks.json is
read and written from processes that share no Python state. This is
the only level that proves the JSON store is consistent under
cross-process concurrency (NFR-08) and that --json / --name plumbing
is wired end-to-end (NFR-04 / FR-01).
"""
