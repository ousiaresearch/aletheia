"""Shared helpers for Weft server modules."""

from __future__ import annotations

import os


def _db_path() -> str:
    return os.environ.get("WEFT_DB_PATH", os.path.expanduser("~/.weft/weft.sqlite3"))


def scope_engine_for(scope: str):
    """Per-request engine that knows every configured scope.

    Authorization is unchanged: the caller's context grants only the requested scope.
    """
    from fastapi import HTTPException
    from weft.scope import ScopeEngine
    known = known_scopes()
    if known is None or scope not in known:
        raise HTTPException(status_code=403, detail="scope not permitted")
    return ScopeEngine(known_scopes=known)


def known_scopes() -> frozenset[str] | None:
    raw = os.environ.get("WEFT_KNOWN_SCOPES")
    if raw is None:
        return None  # unset: fail closed
    scopes = frozenset(s.strip() for s in raw.split(",") if s.strip())
    if not scopes:
        return None  # empty: fail closed
    return scopes


# ---- shared bearer authentication (single implementation) -----------------

import hmac as _hmac

# Maximum and minimum recall result count accepted over HTTP.
MAX_RECALL_LIMIT = 100


def raw_authorization(headers) -> bytes:
    """The raw Authorization header bytes from ASGI ``(name, value)`` pairs.

    One interpretation for every caller: bytes as sent, never a latin-1 or other
    decoded string. When several are sent, the last wins (as ``dict(headers)`` did).
    """
    value = b""
    for name, val in headers or ():
        if name.lower() == b"authorization":
            value = val
    return value


def check_bearer(authorization: bytes | str | None) -> tuple[int, str] | None:
    """Return None when *authorization* carries the configured API key.

    Otherwise return (401, message). Fails closed when WEFT_API_KEY is unset or
    empty, when the header is not valid UTF-8, or when it is not ``Bearer <key>``.
    The comparison is between the UTF-8 bytes of the key and the raw header bytes.
    """
    api_key = os.environ.get("WEFT_API_KEY", "")
    if not api_key:
        return 401, "missing bearer token"
    if isinstance(authorization, str):
        authorization = authorization.encode("utf-8")
    raw = authorization or b""
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return 401, "invalid token"
    if not raw.startswith(b"Bearer "):
        return 401, "missing bearer token"
    token = raw.split(b" ", 1)[1].strip()
    if not _hmac.compare_digest(token, api_key.encode("utf-8")):
        return 401, "invalid token"
    return None


def require_api_key(request) -> None:
    """FastAPI helper: raise HTTP 401 unless the request carries the API key."""
    from fastapi import HTTPException
    failure = check_bearer(raw_authorization(request.scope.get("headers", ())))
    if failure is not None:
        raise HTTPException(status_code=failure[0], detail=failure[1])


class AuthMiddleware:
    """ASGI bearer-auth middleware. Denies every scope type except ``http``
    (authenticated) and ``lifespan`` (server startup/shutdown, no client data).
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        stype = scope["type"]
        if stype == "lifespan":
            await self.app(scope, receive, send)
            return
        if stype == "http":
            failure = check_bearer(raw_authorization(scope.get("headers", [])))
            if failure is None:
                await self.app(scope, receive, send)
                return
            from starlette.responses import Response
            await Response(failure[1], status_code=failure[0])(scope, receive, send)
            return
        if stype == "websocket":
            # Refuse the handshake; never reach the application.
            await send({"type": "websocket.close", "code": 1008})
        # Unknown scope types are dropped without reaching the application.
