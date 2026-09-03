"""Environment reading that treats a blank value as absent.

`os.getenv("X", default)` returns the default only when X is *unset*. An X that is
set-but-empty returns `""`, which then defeats the default — and `.env.example`
ships a dozen variables written exactly that way (`DECISION_WAL_DIR=`), so this is
the normal case rather than an edge case.

The failures that produced this module:

* `DECISION_WAL_DIR=` made `_dir()` call `os.makedirs("")`, which raises
  FileNotFoundError, so with `DECISION_WAL=1` **every decision 500'd**.
* `int(os.getenv("DB_POOL_SIZE", "20"))` raises ValueError on a blank value, which
  would take the process down at startup rather than fall back to 20.
* A blank `RULEMIND_ADMIN_JWT_SECRET` would sign admin tokens with an empty key
  instead of the intended fallback.
"""
from __future__ import annotations

import os
from typing import List, Optional

_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off"}


def env_str(name: str, default: str = "") -> str:
    """The value of `name`, or `default` when it is unset OR blank."""
    raw = os.getenv(name)
    if raw is None:
        return default
    raw = raw.strip()
    return raw if raw else default


def env_int(name: str, default: int) -> int:
    """Integer value, falling back to `default` when unset, blank or unparseable.
    A malformed number is a misconfiguration, not a reason to fail to boot."""
    raw = env_str(name)
    if not raw:
        return default
    try:
        return int(float(raw))
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    raw = env_str(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def env_bool(name: str, default: bool = False) -> bool:
    raw = env_str(name).lower()
    if raw in _TRUTHY:
        return True
    if raw in _FALSY:
        return False
    return default


def env_opt(name: str) -> Optional[str]:
    """The value, or None when unset or blank — for genuinely optional settings
    where blank must mean "not configured" rather than an empty configured value."""
    raw = env_str(name)
    return raw or None


def env_list(name: str, separator: str = ",") -> List[str]:
    """Separator-delimited values with blanks dropped.

    `"a,,b,".split(",")` yields two empty strings; for a list of *secrets*
    (RULEMIND_CONFIG_KEY_OLD) an empty entry would derive a key from the empty
    string — publicly computable — and quietly join the decryption chain. Blank
    entries are dropped rather than carried."""
    return [item.strip() for item in env_str(name).split(separator) if item.strip()]
