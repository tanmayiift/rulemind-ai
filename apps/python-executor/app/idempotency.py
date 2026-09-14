"""Idempotency-Key support for public writes (Track B6).

A client that retries a POST after a timeout cannot know whether the first attempt was
applied. Without a key, the safe-looking retry silently duplicates the write; with one, the
server replays the original response instead of executing again.

Opt-in per request, the way Stripe and every other API that ships this does it: no header,
no behaviour change. That matters for adoption -- making it mandatory would break every
existing integration on upgrade.

WHAT IS AND IS NOT REPLAYED, which is the part usually got wrong:

  * 2xx and 4xx ARE remembered. Both are deterministic outcomes of the request; replaying a
    422 is just as correct as replaying a 201, and not caching 4xx would let a client retry
    its way around validation.
  * 5xx is NOT remembered -- the row is released instead. A 500 is usually transient (a
    database blip, a timeout), so caching it would pin a client to a failure that has already
    resolved and make the key unusable for its actual purpose.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Optional

# How long a key is honoured. 24h matches the common convention and, more practically, is
# comfortably longer than any client's retry window while short enough that the table stays
# small without a dedicated reaper.
DEFAULT_TTL_HOURS = 24

# Only these methods can be made idempotent. GET/HEAD/OPTIONS are already idempotent by
# definition, and applying the machinery to them would add a write to every read.
IDEMPOTENT_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

HEADER = "idempotency-key"
MAX_KEY_LENGTH = 255


def request_fingerprint(method: str, path: str, body: bytes) -> str:
    """Identifies the request a key was issued for.

    Method and path are included, not just the body: the same JSON posted to two different
    endpoints is two different operations, and a key reused across them must be rejected
    rather than served the first endpoint's response.
    """
    digest = hashlib.sha256()
    digest.update(method.upper().encode("utf-8"))
    digest.update(b"\x00")
    digest.update(path.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(body or b"")
    return digest.hexdigest()


def is_expired(created_at: Optional[datetime], ttl_hours: int = DEFAULT_TTL_HOURS,
               now: Optional[datetime] = None) -> bool:
    if created_at is None:
        return True
    return (now or datetime.utcnow()) - created_at > timedelta(hours=ttl_hours)


def should_remember(status_code: int) -> bool:
    """5xx is released rather than remembered -- see the module docstring."""
    return status_code < 500
