"""FastAPI routers — cohesive slices of the HTTP API extracted from the former monolith
``app/main.py``.

Routers reference shared app state through the ``app.main`` module object (``main.storage``,
``main.active_tenant_id``, ``main.ensure_exists``) resolved **at call time**, never captured by
value at import. This matters because the test harness swaps ``app.main.storage`` for an isolated
per-test database (``app_main.storage = Storage(path=…)``); handlers that read the module global
live pick up the swap, exactly as the in-line endpoints did, while a value-bound
``from .main import storage`` would keep pointing at the original singleton. ``main`` includes the
routers at the bottom, after those names exist, so the partial-module import resolves cleanly."""
