"""Phase 6: shadow-mode read path with journal isolation."""

from __future__ import annotations

import difflib
import json
import os
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from weft.substrate import Substrate, Record
from weft.scope import ScopeEngine, ScopeContext
from weft.server_common import known_scopes as _known_scopes, _db_path


class ShadowRecallRequest(BaseModel):
    scope: str
    query: str
    limit: int = 5


class ShadowReasonRequest(BaseModel):
    scope: str
    query: str


def _shadow_ctx(scope: str) -> tuple[ScopeEngine, ScopeContext]:
    known = _known_scopes()
    if known is None or scope not in known:
        raise HTTPException(status_code=403, detail="scope not permitted")
    engine = ScopeEngine(known_scopes={scope})
    ctx = ScopeContext(granted_scopes=frozenset({scope}), user_authored=False, actor="shadow")
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


def _diff_records(active: list[dict], shadow: list[dict]) -> dict[str, Any]:
    active_ids = {r["record_id"] for r in active}
    shadow_ids = {r["record_id"] for r in shadow}
    added = sorted(shadow_ids - active_ids)
    removed = sorted(active_ids - shadow_ids)
    common = sorted(active_ids & shadow_ids)
    return {
        "added": added,
        "removed": removed,
        "common": common,
        "active_count": len(active),
        "shadow_count": len(shadow),
    }


def _append(app: FastAPI) -> None:
    @app.post("/shadow/recall")
    def shadow_recall(request: Request, req: ShadowRecallRequest) -> JSONResponse:
        _require_api_key(request)
        engine, ctx = _shadow_ctx(req.scope)
        sub = Substrate(_db_path(), engine)
        try:
            readable, _ = sub.query(ctx)
            q = req.query.lower()
            matches = []
            for r in readable:
                text = r.payload.get("text", "") or ""
                if q in text.lower():
                    matches.append({
                        "record_id": r.record_id,
                        "record_type": r.record_type,
                        "text": text[:500],
                        "effective_at": r.effective_at,
                        "supersedes": r.supersedes,
                    })
            matches = matches[: req.limit]
            return JSONResponse({
                "matches": matches,
                "total": len(matches),
                "actor": "shadow",
                "user_authored": False,
            })
        finally:
            sub.close()

    @app.post("/shadow/reason")
    def shadow_reason(request: Request, req: ShadowReasonRequest) -> JSONResponse:
        _require_api_key(request)
        engine, ctx = _shadow_ctx(req.scope)
        sub = Substrate(_db_path(), engine)
        try:
            readable, _ = sub.query(ctx)
            q = req.query.lower()
            evidence = []
            for r in readable:
                text = r.payload.get("text", "") or ""
                if q in text.lower():
                    evidence.append({
                        "record_id": r.record_id,
                        "text": text[:800],
                        "epistemic_status": r.epistemic_status,
                    })
            evidence = evidence[:5]
            return JSONResponse({
                "query": req.query,
                "evidence": evidence,
                "actor": "shadow",
                "user_authored": False,
                "note": "shadow reasoning does not write to the active session journal",
            })
        finally:
            sub.close()

    @app.post("/shadow/compare")
    def shadow_compare(request: Request, req: ShadowRecallRequest) -> JSONResponse:
        _require_api_key(request)
        engine_active, ctx_active = _ctx_for(req.scope)
        engine_shadow, ctx_shadow = _shadow_ctx(req.scope)
        sub_active = Substrate(_db_path(), engine_active)
        sub_shadow = Substrate(_db_path(), engine_shadow)
        try:
            readable_active, _ = sub_active.query(ctx_active)
            readable_shadow, _ = sub_shadow.query(ctx_shadow)
            q = req.query.lower()

            def _filter(records):
                out = []
                for r in records:
                    text = r.payload.get("text", "") or ""
                    if q in text.lower():
                        out.append({
                            "record_id": r.record_id,
                            "record_type": r.record_type,
                            "text": text[:500],
                            "effective_at": r.effective_at,
                            "supersedes": r.supersedes,
                        })
                return out

            active_matches = _filter(readable_active)[: req.limit]
            shadow_matches = _filter(readable_shadow)[: req.limit]
            diff = _diff_records(active_matches, shadow_matches)
            return JSONResponse({
                "active": active_matches,
                "shadow": shadow_matches,
                "diff": diff,
            })
        finally:
            sub_active.close()
            sub_shadow.close()


def _ctx_for(scope: str, actor: str = "local-user") -> tuple[ScopeEngine, ScopeContext]:
    known = _known_scopes()
    if known is None or scope not in known:
        raise HTTPException(status_code=403, detail="scope not permitted")
    engine = ScopeEngine(known_scopes={scope})
    ctx = ScopeContext(granted_scopes=frozenset({scope}), user_authored=True, actor=actor)
    return engine, ctx
