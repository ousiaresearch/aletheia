"""Phase 6: local HTTP service for desktop integration."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from weft.substrate import Substrate, Record
from weft.scope import ScopeEngine, ScopeContext
from weft.shadow import _append as _append_shadow

logger = logging.getLogger("weft.server")
app = FastAPI(title="Weft Memory", version="0.1.0a0", docs_url=None, openapi_url=None)


from weft.server_common import AuthMiddleware as _AuthMiddleware, require_api_key as _require_api_key, MAX_RECALL_LIMIT  # noqa: E402


app.add_middleware(_AuthMiddleware)


class QueryRequest(BaseModel):
    scope: str
    query: str
    limit: int = Field(default=5, ge=1, le=MAX_RECALL_LIMIT)


class ExportRequest(BaseModel):
    scope: str


from weft.server_common import known_scopes as _known_scopes, scope_engine_for as _scope_engine_for, _db_path


def _ctx_for(scope: str) -> tuple[ScopeEngine, ScopeContext]:
    engine = _scope_engine_for(scope)
    ctx = ScopeContext(granted_scopes=frozenset({scope}), user_authored=False, actor="http-client")
    return engine, ctx


@app.get("/health")
def health(request: Request) -> dict[str, str]:
    _require_api_key(request)
    return {"status": "ok"}


@app.post("/query")
def query(request: Request, req: QueryRequest) -> JSONResponse:
    _require_api_key(request)
    engine, ctx = _ctx_for(req.scope)
    sub = Substrate(_db_path(), engine)
    try:
        readable, _ = sub.query(ctx)
        # naive lexical filter for alpha
        q = req.query.lower()
        matches = [r for r in readable if q in (r.payload.get("text", "") or "").lower()]
        results = []
        for r in matches[: req.limit]:
            results.append({
                "record_id": r.record_id,
                "record_type": r.record_type,
                "text": r.payload.get("text", ""),
                "effective_at": r.effective_at,
                "supersedes": r.supersedes,
            })
        return JSONResponse({"matches": results, "total": len(matches)})
    finally:
        sub.close()


@app.post("/export")
def export_scope(request: Request, req: ExportRequest) -> JSONResponse:
    _require_api_key(request)
    engine, ctx = _ctx_for(req.scope)
    sub = Substrate(_db_path(), engine)
    try:
        data = sub.export_scope(req.scope, ctx)
        return JSONResponse(data)
    finally:
        sub.close()


@app.get("/record/{record_id}")
def get_record(request: Request, record_id: str, scope: str) -> JSONResponse:
    _require_api_key(request)
    engine, ctx = _ctx_for(scope)
    sub = Substrate(_db_path(), engine)
    try:
        rec = sub.get(record_id, ctx)
        if rec is None:
            raise HTTPException(status_code=404, detail="record not found or not readable")
        return JSONResponse({
            "record_id": rec.record_id,
            "record_type": rec.record_type,
            "scope": rec.scope,
            "epistemic_status": rec.epistemic_status,
            "payload": rec.payload,
            "effective_at": rec.effective_at,
            "supersedes": rec.supersedes,
            "source_ids": rec.source_ids,
            "provenance": rec.provenance,
            "attribution": rec.attribution,
        })
    finally:
        sub.close()


_append_shadow(app)

# The deployment serves both the desktop API and the custom HTTP tool routes.
from weft.mcp_server import app as _tool_app
app.include_router(_tool_app.router)


def main(argv: list[str] | None = None) -> int:
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(description="Weft Memory Server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
