from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Iterator, Optional


_current_tenant_id: ContextVar[Optional[str]] = ContextVar("rulemind_current_tenant_id", default=None)
_current_api_key_id: ContextVar[Optional[str]] = ContextVar("rulemind_current_api_key_id", default=None)
_current_platform_admin_id: ContextVar[Optional[str]] = ContextVar("rulemind_current_platform_admin_id", default=None)
_current_role: ContextVar[Optional[str]] = ContextVar("rulemind_current_role", default=None)


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
