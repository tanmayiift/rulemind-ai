from __future__ import annotations

import re
import uuid
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Iterator, Optional


_current_tenant_id: ContextVar[Optional[str]] = ContextVar("rulemind_current_tenant_id", default=None)
_current_api_key_id: ContextVar[Optional[str]] = ContextVar("rulemind_current_api_key_id", default=None)
_current_platform_admin_id: ContextVar[Optional[str]] = ContextVar("rulemind_current_platform_admin_id", default=None)
_current_role: ContextVar[Optional[str]] = ContextVar("rulemind_current_role", default=None)
_current_request_id: ContextVar[Optional[str]] = ContextVar("rulemind_current_request_id", default=None)

# The canonical inbound/outbound header. Kept here (not in middleware) so the logging
# filter, the middleware and any client helper cannot drift on the spelling.
REQUEST_ID_HEADER = "X-Request-Id"

# An inbound id is attacker-controlled text that ends up in every log line for the request.
# Unrestricted, it is a log-injection vector (newlines forge whole log records) and an
# unbounded-length field. Accept only what a trace/correlation id ever legitimately is.
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def new_request_id() -> str:
    return uuid.uuid4().hex


def sanitize_request_id(value: Optional[str]) -> Optional[str]:
    """Return the inbound id if it is safe to echo and to log, else None.

    None means "generate a fresh one" — never "trust it anyway". A caller that sends a
    100KB id, or one containing a newline, gets a server-generated id instead.
    """
    if not value:
        return None
    candidate = value.strip()
    return candidate if _SAFE_REQUEST_ID.match(candidate) else None


def get_current_request_id() -> Optional[str]:
    return _current_request_id.get()


def set_current_request_id(value: Optional[str]) -> Token:
    return _current_request_id.set(value)


def reset_current_request_id(token: Token) -> None:
    _current_request_id.reset(token)


@contextmanager
def request_id_scope(value: Optional[str] = None) -> Iterator[str]:
    """Bind a request id for the duration of the block, yielding the id actually used.

    `value` is treated as untrusted inbound input: it is accepted only if it passes
    `sanitize_request_id`, otherwise a fresh id is generated.
    """
    resolved = sanitize_request_id(value) or new_request_id()
    token = set_current_request_id(resolved)
    try:
        yield resolved
    finally:
        reset_current_request_id(token)


def get_current_role() -> Optional[str]:
    return _current_role.get()


def set_current_role(value: Optional[str]) -> Token:
    return _current_role.set(value)


def reset_current_role(token: Token) -> None:
    _current_role.reset(token)


def get_current_tenant_id() -> Optional[str]:
    return _current_tenant_id.get()


def set_current_tenant_id(value: Optional[str]) -> Token:
    return _current_tenant_id.set(value)


def reset_current_tenant_id(token: Token) -> None:
    _current_tenant_id.reset(token)


def get_current_api_key_id() -> Optional[str]:
    return _current_api_key_id.get()


def set_current_api_key_id(value: Optional[str]) -> Token:
    return _current_api_key_id.set(value)


def reset_current_api_key_id(token: Token) -> None:
    _current_api_key_id.reset(token)


def get_current_platform_admin_id() -> Optional[str]:
    return _current_platform_admin_id.get()


def set_current_platform_admin_id(value: Optional[str]) -> Token:
    return _current_platform_admin_id.set(value)


def reset_current_platform_admin_id(token: Token) -> None:
    _current_platform_admin_id.reset(token)


@contextmanager
def tenant_scope(tenant_id: Optional[str], api_key_id: Optional[str] = None) -> Iterator[None]:
    tenant_token = set_current_tenant_id(tenant_id)
    api_key_token = set_current_api_key_id(api_key_id)
    try:
        yield
    finally:
        reset_current_api_key_id(api_key_token)
        reset_current_tenant_id(tenant_token)
