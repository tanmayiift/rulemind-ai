"""One paging contract for every collection endpoint.

THE GAP. Of the 30 GET endpoints that return a list, 4 paginated (`/audit`, `/audit/decisions`,
`/audit/errors`, `/executions`, all in SQL) and **26 returned a bare, unbounded array** --
`/policies`, `/rules`, `/variables`, `/scorecards`, `/reviews`, `/decision-tables`,
`/schedules/{id}/history`, `/bundles/versions` among them. A client had no way to ask for less,
no way to learn how many there were, and no way to walk a large collection a page at a time.

WHY IT IS DONE HERE AND NOT IN 26 HANDLERS. Two reasons, and the second is the one that matters.

  * Uniformity. A per-handler `limit`/`offset` would be 26 hand-written copies of the same
    bounds, the same defaults and the same headers, and they would drift -- this codebase has
    fixed that shape of divergence many times over. One wrapper cannot disagree with itself.
  * It covers the endpoint someone adds next month. `install()` walks the routing table at
    startup, so a new list endpoint is paginated the day it lands, and
    `test_every_list_endpoint_pages` enumerates the same table -- nothing to remember to extend.

THE CONTRACT, which is additive -- a request with no paging parameters gets exactly the response
it got before, so the dashboard's pickers (which load full lists) and all five SDKs are unaffected:

  ?limit=N         1..1000. The page size.
  ?offset=M        >= 0. Items to skip.
  X-Total-Count    the size of the whole collection, on every paged response.
  Link             RFC 8288 `rel="next"` / `rel="prev"` URLs, so a client follows links instead
                   of doing arithmetic.

An invalid value is a 422 in the standard error envelope, not a silent clamp: `?limit=-1` reached
Postgres as `LIMIT -1` on the audit routes and 500'd, and on SQLite it meant NO limit.

WHAT THIS DOES NOT DO, stated so it is not mistaken for more than it is: it bounds what is SENT,
not what is READ. The handler still loads the collection and the slice happens here. For the
authoring collections (rules, variables, policies -- hundreds per tenant, not millions) that is
the right trade. The collections that genuinely grow without bound -- decisions, the audit trail,
executions -- already page in SQL, and this module leaves them alone (see `_declares_paging`).
"""
from __future__ import annotations

import json
import typing
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlencode

from fastapi import FastAPI
from fastapi.routing import APIRoute
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .errors import error_body

MAX_LIMIT = 1000

# (METHOD, path template) of every route this module wraps -- read by the OpenAPI patch below and
# by the test that enumerates coverage.
WRAPPED: Set[Tuple[str, str]] = set()


def _returns_list(route: APIRoute) -> bool:
    """Does the endpoint's return annotation say it returns a list?"""
    import inspect

    annotation = inspect.signature(route.endpoint).return_annotation
    origin = typing.get_origin(annotation)
    if origin in (list, List):
        return True
    # `from __future__ import annotations` leaves some annotations as strings.
    return isinstance(annotation, str) and annotation.replace(" ", "").startswith(("List[", "list["))


def _declares_paging(route: APIRoute) -> bool:
    """The endpoint pages ITSELF (in SQL). Wrapping it would apply the offset twice: the handler
    returns rows 200-399 for ?offset=200 and a second slice of [200:400] would return nothing."""
    import inspect

    params = inspect.signature(route.endpoint).parameters
    return "limit" in params or "offset" in params


def _parse(query: Dict[str, str], name: str, low: int, high: Optional[int]) -> Tuple[Optional[int], Optional[str]]:
    raw = query.get(name)
    if raw is None or raw == "":
        return None, None
    try:
        value = int(raw)
    except ValueError:
        return None, f"`{name}` must be an integer, got {raw!r}."
    if value < low or (high is not None and value > high):
        bound = f"between {low} and {high}" if high is not None else f"at least {low}"
        return None, f"`{name}` must be {bound}, got {value}."
    return value, None


def _link_header(path: str, query: Dict[str, str], limit: int, offset: int, total: int) -> str:
    def url(new_offset: int) -> str:
        params = dict(query)
        params["limit"] = str(limit)
        params["offset"] = str(new_offset)
        return f"<{path}?{urlencode(params)}>"

    links = []
    if offset + limit < total:
        links.append(f'{url(offset + limit)}; rel="next"')
    if offset > 0:
        links.append(f'{url(max(0, offset - limit))}; rel="prev"')
    return ", ".join(links)


def _wrap(inner: ASGIApp) -> ASGIApp:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await inner(scope, receive, send)
            return
        query: Dict[str, str] = {}
        for pair in (scope.get("query_string") or b"").decode("latin-1").split("&"):
            if pair:
                key, _, value = pair.partition("=")
                from urllib.parse import unquote_plus

                query[unquote_plus(key)] = unquote_plus(value)

        limit, limit_error = _parse(query, "limit", 1, MAX_LIMIT)
        offset, offset_error = _parse(query, "offset", 0, None)
        problem = limit_error or offset_error
        if problem:
            body = json.dumps(error_body(422, problem)).encode()
            await send({"type": "http.response.start", "status": 422,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})
            return
        if limit is None and offset is None:
            # No paging asked for: exactly the old response. This is the compatibility promise.
            await inner(scope, receive, send)
            return

        start_message: Optional[Message] = None
        chunks: List[bytes] = []

        async def capture(message: Message) -> None:
            nonlocal start_message
            if message["type"] == "http.response.start":
                start_message = message
            elif message["type"] == "http.response.body":
                chunks.append(message.get("body", b""))

        await inner(scope, receive, capture)
        assert start_message is not None
        raw = b"".join(chunks)
        status = start_message["status"]
        try:
            payload = json.loads(raw) if raw and 200 <= status < 300 else None
        except ValueError:
            payload = None
        if not isinstance(payload, list):
            # An error, or not an array after all: pass it through untouched.
            await send(start_message)
            await send({"type": "http.response.body", "body": raw})
            return

        total = len(payload)
        begin = offset or 0
        size = limit if limit is not None else MAX_LIMIT
        page = payload[begin:begin + size]
        body = json.dumps(page, default=str).encode()
        headers = [(k, v) for k, v in start_message.get("headers", [])
                   if k.lower() not in (b"content-length", b"x-total-count", b"link")]
        headers.append((b"content-length", str(len(body)).encode()))
        headers.append((b"x-total-count", str(total).encode()))
        link = _link_header(scope.get("path", ""), query, size, begin, total)
        if link:
            headers.append((b"link", link.encode()))
        await send({**start_message, "headers": headers})
        await send({"type": "http.response.body", "body": body})

    return app


def install(app: FastAPI) -> None:
    """Wrap every GET collection endpoint that does not page itself. Call AFTER every router is
    included -- it walks the routing table as it stands at that moment."""
    for route in app.routes:
        if not isinstance(route, APIRoute) or "GET" not in route.methods:
            continue
        if not _returns_list(route) or _declares_paging(route):
            continue
        route.app = _wrap(route.app)
        WRAPPED.add(("GET", route.path))

    original_openapi = app.openapi

    def openapi() -> Dict[str, Any]:
        """Advertise the parameters in the spec too. Applied at the ASGI layer they would be
        invisible to anyone generating a client from /api/v1/openapi.json -- which is exactly
        the audience that most needs to know a collection can be paged."""
        if app.openapi_schema:
            return app.openapi_schema
        schema = original_openapi()
        for method, path in WRAPPED:
            operation = schema.get("paths", {}).get(path, {}).get(method.lower())
            if operation is None:
                continue
            existing = {p.get("name") for p in operation.get("parameters", [])}
            params = operation.setdefault("parameters", [])
            if "limit" not in existing:
                params.append({"name": "limit", "in": "query", "required": False,
                               "description": "Page size. Omit for the whole collection.",
                               "schema": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT}})
            if "offset" not in existing:
                params.append({"name": "offset", "in": "query", "required": False,
                               "description": "Items to skip.",
                               "schema": {"type": "integer", "minimum": 0}})
        app.openapi_schema = schema
        return schema

    app.openapi = openapi  # type: ignore[method-assign]
