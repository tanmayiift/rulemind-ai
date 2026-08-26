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
    # Search upward from this file: apps/python-executor/.env then the repo root .env.
    # Both are plausible locations and the repo root is where the README puts it.
    _here = pathlib.Path(__file__).resolve()
    for _candidate in (
        _here.parents[1] / ".env",   # apps/python-executor/.env
        _here.parents[3] / ".env",   # repo root .env
    ):
        if _candidate.is_file():
            load_dotenv(_candidate, override=False)
