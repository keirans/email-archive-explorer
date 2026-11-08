"""Local-API hardening (Spec 08 §3.1).

* No CORS: the UI is same-origin, so other websites can't read API responses.
* Host-header allow-list: blocks DNS-rebinding attacks (a hostile site whose
  domain is re-pointed at 127.0.0.1) with ``421 Misdirected Request``.
* Per-process session token: required on every state-changing request and on
  the file-system routes. It is injected into ``index.html`` and sent by the UI
  as ``X-EAE-Token``; other origins can't read the page, so can't learn it.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterable
from typing import Optional

from fastapi import HTTPException, Request
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

TOKEN_HEADER = "x-eae-token"
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
WILDCARD_BINDS = {"0.0.0.0", "::", ""}


def new_token() -> str:
    return secrets.token_urlsafe(32)


def is_loopback(host: Optional[str]) -> bool:
    return (host or "127.0.0.1").strip("[]") in LOOPBACK_HOSTS


def _hostname(host_header: str) -> str:
    """Host header → bare hostname (handles ``[::1]:8000`` and ``host:port``)."""
    h = host_header.strip().lower()
    if h.startswith("["):
        return h[1:h.find("]")] if "]" in h else h
    return h.rsplit(":", 1)[0] if h.count(":") == 1 else h


class HostCheckMiddleware:
    """Reject requests whose Host header isn't an allowed name (``None`` = allow all)."""

    def __init__(self, app: ASGIApp, allowed_hosts: Optional[Iterable[str]]):
        self.app = app
        self.allowed: Optional[set[str]] = (
            {h.strip("[]").lower() for h in allowed_hosts} if allowed_hosts is not None else None
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket") and self.allowed is not None:
            host = ""
            for key, value in scope.get("headers", []):
                if key == b"host":
                    host = value.decode("latin-1")
                    break
            if _hostname(host) not in self.allowed:
                response = PlainTextResponse("Misdirected Request: unexpected Host header", status_code=421)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def require_token(request: Request) -> None:
    """FastAPI dependency: the caller must present this process's session token."""
    expected = request.app.state.token
    supplied = request.headers.get(TOKEN_HEADER, "")
    if not supplied or not secrets.compare_digest(supplied, expected):
        raise HTTPException(status_code=403, detail="Missing or invalid session token")
