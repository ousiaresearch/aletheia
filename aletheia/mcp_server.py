"""Custom HTTP tool routes for Aletheia; not an MCP protocol server."""

from __future__ import annotations

import importlib
import json
import os
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from aletheia.substrate import Substrate, Record, ScopeViolation, SchemaViolation, RecordConflict
from aletheia.scope import ScopeEngine, ScopeContext
from aletheia.server_common import known_scopes as _known_scopes, scope_engine_for as _scope_engine_for, _db_path

logger = __import__("logging").getLogger("aletheia.mcp_server")
app = FastAPI(title="Aletheia MCP", version="0.1.0a0", docs_url=None, openapi_url=None)


from aletheia.server_common import AuthMiddleware as _AuthMiddleware, require_api_key as _require_api_key, MAX_RECALL_LIMIT  # noqa: E402


app.add_middleware(_AuthMiddleware)


class IngestRequest(BaseModel):
    scope: str
    records: list[dict[str, Any]]


class RecallRequest(BaseModel):
    scope: str
    query: str
    limit: int = Field(default=5, ge=1, le=MAX_RECALL_LIMIT)


class StatusRequest(BaseModel):
    scope: str


class MCPCall(BaseModel):
    tool: str
    arguments: dict[str, Any] = {}


def _ctx_for(scope: str) -> tuple[ScopeEngine, ScopeContext]:
    engine = _scope_engine_for(scope)
    ctx = ScopeContext(granted_scopes=frozenset({scope}), user_authored=False, actor="http-client")
    return engine, ctx


@app.get("/health")
def health(request: Request) -> dict[str, str]:
    _require_api_key(request)
    return {"status": "ok"}


def _recall_http(sub: Substrate, scope: str, ctx: ScopeContext, query: str, limit: int):
    """Lexical recall with a documented HTTP error contract."""
    retrieval = importlib.import_module("aletheia.retrieval")
    try:
        return retrieval.recall(
            sub, query, ctx, scope_hint=scope, limit=limit,
            mode="lexical", embedder=None, use_cross_encoder=False,
        )
    except retrieval.IndexIncompleteError:
        raise HTTPException(
            status_code=503,
            detail=(f"full-text index for scope {scope!r} is incomplete or stale; "
                    f"an operator must run: aletheia --db <path> rebuild-fts --scope {scope}"),
        ) from None


def _ingest_records(sub: Substrate, scope: str, ctx: ScopeContext, records: list[dict[str, Any]]) -> JSONResponse:
    try:
        with sub.ingestion_transaction():
            for rec in records:
                try:
                    # JSON responses and SQLite require finite numbers and valid UTF-8.
                    json.dumps(rec, ensure_ascii=False, allow_nan=False).encode("utf-8")
                except (ValueError, TypeError, UnicodeEncodeError, RecursionError):
                    raise HTTPException(status_code=400, detail="invalid record") from None
                payload = rec.get("payload", {})
                if (not isinstance(payload, dict) or not isinstance(rec.get("provenance", {}), dict)
                        or not isinstance(payload.get("text", ""), str)):
                    raise HTTPException(status_code=400, detail="invalid record")
                record = Record(
                    record_id=str(rec.get("record_id") or ""),
                    record_type=str(rec.get("record_type", "Source")),
                    scope=scope,
                    epistemic_status=rec.get("epistemic_status", "stated"),
                    payload=rec.get("payload", {}),
                    provenance=rec.get("provenance", {"source": "mcp"}),
                    attribution="agent",
                )
                sub.put(record, ctx)
    except (ScopeViolation, SchemaViolation, RecordConflict):
        raise HTTPException(status_code=403, detail="write not permitted") from None
    return JSONResponse({"status": "ok", "inserted": len(records)})


@app.post("/mcp/ingest")
def mcp_ingest(request: Request, req: IngestRequest) -> JSONResponse:
    _require_api_key(request)
    engine, ctx = _ctx_for(req.scope)
    db_path = _db_path()
    sub = Substrate(db_path, engine)
    try:
        return _ingest_records(sub, req.scope, ctx, req.records)
    finally:
        sub.close()


@app.post("/mcp/recall")
def mcp_recall(request: Request, req: RecallRequest) -> JSONResponse:
    _require_api_key(request)
    engine, ctx = _ctx_for(req.scope)
    db_path = _db_path()
    sub = Substrate(db_path, engine)
    try:
        items, receipt = _recall_http(sub, req.scope, ctx, req.query, req.limit)
        results = []
        for item in items:
            results.append({
                "record_id": item.record_id,
                "text": item.text,
                
                "tier": item.tier,
                "layers": receipt.layers,
            })
        return JSONResponse({"results": results, "total": len(results)})
    finally:
        sub.close()


@app.post("/mcp/status")
def mcp_status(request: Request, req: StatusRequest) -> JSONResponse:
    _require_api_key(request)
    engine, ctx = _ctx_for(req.scope)
    db_path = _db_path()
    sub = Substrate(db_path, engine)
    try:
        readable, _ = sub.query(ctx)
        return JSONResponse({
            "scope": req.scope,
            "record_count": len(readable),
            "status": "ok",
        })
    finally:
        sub.close()


@app.post("/mcp/call")
def mcp_call(request: Request, req: MCPCall) -> JSONResponse:
    _require_api_key(request)
    tool = req.tool
    args = req.arguments or {}
    scope = args.get("scope", "")
    if not isinstance(scope, str) or not scope:
        raise HTTPException(status_code=400, detail="invalid arguments")
    if tool == "ingest":
        records = args.get("records", [])
        if not isinstance(records, list) or any(not isinstance(rec, dict) for rec in records):
            raise HTTPException(status_code=400, detail="invalid arguments")
    if tool == "recall":
        if not isinstance(args.get("query", ""), str) or type(args.get("limit", 5)) is not int \
                or not 1 <= args.get("limit", 5) <= MAX_RECALL_LIMIT:
            raise HTTPException(status_code=400, detail="invalid arguments")
    engine, ctx = _ctx_for(scope)
    db_path = _db_path()
    sub = Substrate(db_path, engine)
    try:
        if tool == "ingest":
            return _ingest_records(sub, scope, ctx, args.get("records", []))
        elif tool == "recall":
            query = args.get("query", "")
            limit = int(args.get("limit", 5))
            items, receipt = _recall_http(sub, scope, ctx, query, limit)
            results = [
                {"record_id": item.record_id, "text": item.text,  "tier": item.tier}
                for item in items
            ]
            return JSONResponse({"results": results, "total": len(results)})
        elif tool == "status":
            readable, _ = sub.query(ctx)
            return JSONResponse({"scope": scope, "record_count": len(readable), "status": "ok"})
        elif tool == "export":
            data = sub.export_scope(scope, ctx)
            return JSONResponse(data)
        else:
            raise HTTPException(status_code=400, detail=f"unknown tool: {tool}")
    finally:
        sub.close()
