"""Phase 10/14: MCP server wrapping Weft recall/ingest/status."""

from __future__ import annotations

import importlib
import json
import os
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from weft.substrate import Substrate, Record
from weft.scope import ScopeEngine, ScopeContext
from weft.server_common import known_scopes as _known_scopes, _db_path

logger = __import__("logging").getLogger("weft.mcp_server")
app = FastAPI(title="Weft MCP", version="0.1.0a0", docs_url=None, openapi_url=None)


class IngestRequest(BaseModel):
    scope: str
    records: list[dict[str, Any]]


class RecallRequest(BaseModel):
    scope: str
    query: str
    limit: int = 5


class StatusRequest(BaseModel):
    scope: str


class MCPCall(BaseModel):
    tool: str
    arguments: dict[str, Any] = {}


def _ctx_for(scope: str) -> tuple[ScopeEngine, ScopeContext]:
    known = _known_scopes()
    if known is None or scope not in known:
        raise HTTPException(status_code=403, detail="scope not permitted")
    actor = os.environ.get("WEFT_HTTP_ACTOR")
    if not actor:
        api_key = os.environ.get("WEFT_API_KEY", "")
        actor = "http-client-" + api_key[:8] if api_key else "http-client"
    engine = ScopeEngine(known_scopes={scope})
    ctx = ScopeContext(granted_scopes=frozenset({scope}), user_authored=True, actor=actor)
    return engine, ctx


def _require_api_key(request):
    api_key = os.environ.get("WEFT_API_KEY", "")
    if not api_key:
        raise HTTPException(status_code=401, detail="missing bearer token")
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = auth.split(" ", 1)[1].strip()
    if token != api_key:
        raise HTTPException(status_code=401, detail="invalid token")


def _load_embedder():
    try:
        from sentence_transformers import SentenceTransformer
        model = SentenceTransformer("intfloat/e5-large-v2")
        model_id = f"SentenceTransformer({model.tokenizer.name_or_path})"

        class _ST:
            def __init__(self, model, model_id):
                self._model = model
                self.model_id = model_id

            def embed_query(self, text: str) -> list:
                return self._model.encode(text, normalize_embeddings=True).tolist()

            def embed_document(self, text: str) -> list:
                return self._model.encode(text, normalize_embeddings=True).tolist()

            def embed_documents(self, texts: list[str]) -> list:
                return self._model.encode(texts, normalize_embeddings=True).tolist()

        return _ST(model, model_id)
    except Exception:
        return None


@app.get("/health")
def health(request: Request) -> dict[str, str]:
    _require_api_key(request)
    return {"status": "ok"}


@app.post("/mcp/ingest")
def mcp_ingest(request: Request, req: IngestRequest) -> JSONResponse:
    _require_api_key(request)
    engine, ctx = _ctx_for(req.scope)
    db_path = _db_path()
    sub = Substrate(db_path, engine)
    try:
        inserted = 0
        for rec in req.records:
            record = Record(
                record_id=str(rec.get("record_id") or ""),
                record_type=str(rec.get("record_type", "Observation")),
                scope=req.scope,
                epistemic_status=rec.get("epistemic_status", "observed"),
                payload=rec.get("payload", {}),
                provenance=rec.get("provenance", {"source": "mcp"}),
                attribution=ctx.actor,
            )
            sub.put(record, ctx)
            inserted += 1
        return JSONResponse({"status": "ok", "inserted": inserted})
    finally:
        sub.close()


@app.post("/mcp/recall")
def mcp_recall(request: Request, req: RecallRequest) -> JSONResponse:
    _require_api_key(request)
    engine, ctx = _ctx_for(req.scope)
    db_path = _db_path()
    sub = Substrate(db_path, engine)
    try:
        embedder = _load_embedder()
        mode = "hybrid" if embedder else "lexical"
        items, receipt = importlib.import_module("weft.retrieval").recall(
            sub, req.query, ctx, scope_hint=req.scope, limit=req.limit,
            mode=mode, embedder=embedder, use_cross_encoder=False,
        )
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
    if not scope:
        raise HTTPException(status_code=400, detail="scope is required")
    engine, ctx = _ctx_for(scope)
    db_path = _db_path()
    sub = Substrate(db_path, engine)
    try:
        if tool == "ingest":
            records = args.get("records", [])
            inserted = 0
            for rec in records:
                record = Record(
                    record_id=str(rec.get("record_id") or ""),
                    record_type=str(rec.get("record_type", "Observation")),
                    scope=scope,
                    epistemic_status=rec.get("epistemic_status", "observed"),
                    payload=rec.get("payload", {}),
                    provenance=rec.get("provenance", {"source": "mcp"}),
                    attribution=ctx.actor,
                )
                sub.put(record, ctx)
                inserted += 1
            return JSONResponse({"status": "ok", "inserted": inserted})
        elif tool == "recall":
            query = args.get("query", "")
            limit = int(args.get("limit", 5))
            embedder = _load_embedder()
            mode = "hybrid" if embedder else "lexical"
            items, receipt = importlib.import_module("weft.retrieval").recall(
                sub, query, ctx, scope_hint=scope, limit=limit,
                mode=mode, embedder=embedder, use_cross_encoder=False,
            )
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
