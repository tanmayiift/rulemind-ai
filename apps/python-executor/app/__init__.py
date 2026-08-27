"""RuleMind FastAPI application package.

Loads a local `.env` before any module reads `os.environ`. Every config value in
this app is read at import time (`app/db.py`, `app/storage.py`, `app/middleware.py`
… all call `os.getenv` at module scope), so this has to happen here — in the
package `__init__` — rather than inside `main.py`, which imports those modules.

`override=False` keeps precedence correct: a real environment variable always wins
over the file, so docker-compose `environment:` blocks and k8s env stay
authoritative and `.env` only fills gaps. Missing file is a silent no-op.
"""
import os
import pathlib

try:
    from dotenv import load_dotenv
except ImportError:  # dotenv is a convenience, never a hard requirement
    load_dotenv = None

if load_dotenv is not None and os.getenv("RULEMIND_SKIP_DOTENV") != "1":
    # Search upward from this file for a .env: the service directory
    # (apps/python-executor/.env) and the repo root are both plausible, and the repo
    # root is where the README puts it.
    #
    # Indexing `parents` directly is not safe. In the Docker image the package sits at
    # /app/app, so there is no parents[3] and a bare index raised IndexError at import
    # time — which meant the API container could not start at all. Walk whatever
    # ancestors actually exist instead, and stop at the first .env found so a service
    # directory file wins over one at the root.
    _here = pathlib.Path(__file__).resolve()
    for _candidate in (parent / ".env" for parent in _here.parents[1:4]):
        if _candidate.is_file():
            load_dotenv(_candidate, override=False)
            break
