"""The one error envelope: `{"error": <stable string>, "detail": <context>}`.

Lives in its own module because BOTH `main.py`'s exception handlers and `middleware.py`'s early
returns have to produce it, and main imports middleware -- so a helper in either one is a cycle
or a duplicate. A duplicate is how the two shapes diverged in the first place.
"""
from __future__ import annotations

from typing import Any, Dict


# Status code -> the stable machine-readable code a client switches on. Deliberately a small
# fixed vocabulary rather than one per call site: a client writes ONE parser, and adding a new
# error message does not invent a new code nobody is handling.
ERROR_CODES = {
    400: "bad_request",
    401: "unauthorized",
    402: "payment_required",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    422: "unprocessable_entity",
    429: "rate_limited",
    500: "internal_error",
    502: "upstream_error",
    503: "unavailable",
    504: "timeout",
}


def error_body(status_code: int, detail: Any) -> Dict[str, Any]:
    """The one error envelope: `{"error": <stable string>, "detail": <context>}`.

    THE PROBLEM. There were two incompatible shapes. The middleware returned
    `{"error": ..., "detail": ...}` (and sometimes only `error`); every route handler raised
    `HTTPException`, which FastAPI renders as `{"detail": ...}` with no `error` at all. And
    `detail` is a string at 187 call sites and a DICT at 3. A client could not write one
    parser: it had to sniff which field was present, then sniff the type of `detail`.

    THE FIX IS ADDITIVE, NOT A FLATTENING, and that is the important part. The obvious
    "normalise everything to a string" would destroy the three structured errors -- and those
    three are the GOOD ones: `invalid_input` carries per-field `issues`, which is the single
    most useful error the product produces, and `idempotency_key_reused` carries the clashing
    key. Seven tests and any integrator depend on that structure.

    So: `error` is guaranteed present and guaranteed a string -- the field to switch on --
    and `detail` keeps whatever shape the handler chose, as context. A structured detail that
    already names its own error code (`{"error": "invalid_input", ...}`) keeps it, so the
    envelope agrees with the body rather than overwriting it with a generic status word.
    """
    code = ERROR_CODES.get(status_code, "error")
    if isinstance(detail, dict):
        inner = detail.get("error")
        if isinstance(inner, str) and inner:
            code = inner
    return {"error": code, "detail": detail}
