"""Side-effect-free helper for the model-worker env-scrub test.

Kept in its own module ON PURPOSE. If the probe lived in the main test module, a spawned worker
would import that module to unpickle the probe -- and its top-level os.environ.setdefault would
re-add RULEMIND_CONFIG_KEY in the child AFTER the worker initializer scrubbed it, making the test
pass for the wrong reason. This module sets nothing at import.
"""
from __future__ import annotations

import os


def read_env(name: str) -> str:
    return os.environ.get(name, "")
