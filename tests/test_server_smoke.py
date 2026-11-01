"""Smoke tests for Weft HTTP server and MCP server."""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from weft.server import app as server_app
from weft.mcp_server import app as mcp_app


class TestServerSmoke:
    def test_health(self):
        client = TestClient(server_app)
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_query_with_api_key(self, monkeypatch):
        monkeypatch.setenv("WEFT_API_KEY", "test-key")
        monkeypatch.setenv("WEFT_KNOWN_SCOPES", "s")
        client = TestClient(server_app)
        resp = client.post(
            "/query",
            json={"scope": "s", "query": "foo"},
            headers={"Authorization": "Bearer test-key"},
        )
        assert resp.status_code == 200

    def test_query_rejects_missing_api_key(self, monkeypatch):
        monkeypatch.setenv("WEFT_API_KEY", "test-key")
        monkeypatch.setenv("WEFT_KNOWN_SCOPES", "s")
        client = TestClient(server_app)
        resp = client.post(
            "/query",
            json={"scope": "s", "query": "foo"},
        )
        assert resp.status_code == 401

    def test_query_rejects_invalid_api_key(self, monkeypatch):
        monkeypatch.setenv("WEFT_API_KEY", "test-key")
        monkeypatch.setenv("WEFT_KNOWN_SCOPES", "s")
        client = TestClient(server_app)
        resp = client.post(
            "/query",
            json={"scope": "s", "query": "foo"},
            headers={"Authorization": "Bearer wrong"},
        )
        assert resp.status_code == 401

    def test_query_rejects_unknown_scope(self, monkeypatch):
        monkeypatch.setenv("WEFT_API_KEY", "test-key")
        monkeypatch.setenv("WEFT_KNOWN_SCOPES", "s")
        client = TestClient(server_app)
        resp = client.post(
            "/query",
            json={"scope": "unknown", "query": "foo"},
            headers={"Authorization": "Bearer test-key"},
        )
        assert resp.status_code == 403

    def test_query_fails_closed_without_known_scopes(self, monkeypatch):
        monkeypatch.setenv("WEFT_API_KEY", "test-key")
        monkeypatch.delenv("WEFT_KNOWN_SCOPES", raising=False)
        client = TestClient(server_app)
        resp = client.post(
            "/query",
            json={"scope": "s", "query": "foo"},
            headers={"Authorization": "Bearer test-key"},
        )
        assert resp.status_code == 403

    def test_query_fails_closed_with_empty_known_scopes(self, monkeypatch):
        monkeypatch.setenv("WEFT_API_KEY", "test-key")
        monkeypatch.setenv("WEFT_KNOWN_SCOPES", "")
        client = TestClient(server_app)
        resp = client.post(
            "/query",
            json={"scope": "s", "query": "foo"},
            headers={"Authorization": "Bearer test-key"},
        )
        assert resp.status_code == 403

    def test_export_with_api_key(self, monkeypatch):
        monkeypatch.setenv("WEFT_API_KEY", "test-key")
        monkeypatch.setenv("WEFT_KNOWN_SCOPES", "s")
        client = TestClient(server_app)
        resp = client.post(
            "/export",
            json={"scope": "s"},
            headers={"Authorization": "Bearer test-key"},
        )
        assert resp.status_code == 200

    def test_get_record_with_api_key(self, monkeypatch):
        monkeypatch.setenv("WEFT_API_KEY", "test-key")
        monkeypatch.setenv("WEFT_KNOWN_SCOPES", "s")
        client = TestClient(server_app)
        resp = client.get(
            "/record/rec1",
            params={"scope": "s"},
            headers={"Authorization": "Bearer test-key"},
        )
        assert resp.status_code == 404  # record not found, not auth failure

    def test_shadow_recall_with_api_key(self, monkeypatch):
        monkeypatch.setenv("WEFT_API_KEY", "test-key")
        monkeypatch.setenv("WEFT_KNOWN_SCOPES", "s")
        client = TestClient(server_app)
        resp = client.post(
            "/shadow/recall",
            json={"scope": "s", "query": "foo"},
            headers={"Authorization": "Bearer test-key"},
        )
        assert resp.status_code == 200

    def test_shadow_recall_rejects_missing_api_key(self, monkeypatch):
        monkeypatch.setenv("WEFT_API_KEY", "test-key")
        monkeypatch.setenv("WEFT_KNOWN_SCOPES", "s")
        client = TestClient(server_app)
        resp = client.post(
            "/shadow/recall",
            json={"scope": "s", "query": "foo"},
        )
        assert resp.status_code == 401

    def test_shadow_recall_rejects_unknown_scope(self, monkeypatch):
        monkeypatch.setenv("WEFT_API_KEY", "test-key")
        monkeypatch.setenv("WEFT_KNOWN_SCOPES", "s")
        client = TestClient(server_app)
        resp = client.post(
            "/shadow/recall",
            json={"scope": "unknown", "query": "foo"},
            headers={"Authorization": "Bearer test-key"},
        )
        assert resp.status_code == 403

    def test_shadow_recall_fails_closed_without_known_scopes(self, monkeypatch):
        monkeypatch.setenv("WEFT_API_KEY", "test-key")
        monkeypatch.setenv("WEFT_KNOWN_SCOPES", "")
        client = TestClient(server_app)
        resp = client.post(
            "/shadow/recall",
            json={"scope": "s", "query": "foo"},
            headers={"Authorization": "Bearer test-key"},
        )
        assert resp.status_code == 403

    def test_shadow_recall_fails_closed_when_api_key_unset(self, monkeypatch):
        monkeypatch.delenv("WEFT_API_KEY", raising=False)
        monkeypatch.setenv("WEFT_KNOWN_SCOPES", "s")
        client = TestClient(server_app)
        resp = client.post(
            "/shadow/recall",
            json={"scope": "s", "query": "foo"},
        )
        assert resp.status_code == 401

    def test_shadow_reason_rejects_missing_api_key(self, monkeypatch):
        monkeypatch.setenv("WEFT_API_KEY", "test-key")
        monkeypatch.setenv("WEFT_KNOWN_SCOPES", "s")
        client = TestClient(server_app)
        resp = client.post("/shadow/reason", json={"scope": "s", "query": "foo"})
        assert resp.status_code == 401

    def test_shadow_compare_rejects_missing_api_key(self, monkeypatch):
        monkeypatch.setenv("WEFT_API_KEY", "test-key")
        monkeypatch.setenv("WEFT_KNOWN_SCOPES", "s")
        client = TestClient(server_app)
        resp = client.post("/shadow/compare", json={"scope": "s", "query": "foo"})
        assert resp.status_code == 401


class TestMCPsmoke:
    def test_health(self):
        client = TestClient(mcp_app)
        resp = client.get("/health")
        assert resp.status_code == 200

    def test_ingest_with_api_key(self, monkeypatch):
        monkeypatch.setenv("WEFT_API_KEY", "test-key")
        monkeypatch.setenv("WEFT_KNOWN_SCOPES", "s")
        client = TestClient(mcp_app)
        resp = client.post(
            "/mcp/ingest",
            json={"scope": "s", "records": []},
            headers={"Authorization": "Bearer test-key"},
        )
        assert resp.status_code == 200

    def test_ingest_rejects_missing_api_key(self, monkeypatch):
        monkeypatch.setenv("WEFT_API_KEY", "test-key")
        monkeypatch.setenv("WEFT_KNOWN_SCOPES", "s")
        client = TestClient(mcp_app)
        resp = client.post(
            "/mcp/ingest",
            json={"scope": "s", "records": []},
        )
        assert resp.status_code == 401

    def test_ingest_rejects_invalid_api_key(self, monkeypatch):
        monkeypatch.setenv("WEFT_API_KEY", "test-key")
        monkeypatch.setenv("WEFT_KNOWN_SCOPES", "s")
        client = TestClient(mcp_app)
        resp = client.post(
            "/mcp/ingest",
            json={"scope": "s", "records": []},
            headers={"Authorization": "Bearer wrong"},
        )
        assert resp.status_code == 401

    def test_ingest_rejects_unknown_scope(self, monkeypatch):
        monkeypatch.setenv("WEFT_API_KEY", "test-key")
        monkeypatch.setenv("WEFT_KNOWN_SCOPES", "s")
        client = TestClient(mcp_app)
        resp = client.post(
            "/mcp/ingest",
            json={"scope": "unknown", "records": []},
            headers={"Authorization": "Bearer test-key"},
        )
        assert resp.status_code == 403

    def test_ingest_fails_closed_without_known_scopes(self, monkeypatch):
        monkeypatch.setenv("WEFT_API_KEY", "test-key")
        monkeypatch.setenv("WEFT_KNOWN_SCOPES", "")
        client = TestClient(mcp_app)
        resp = client.post(
            "/mcp/ingest",
            json={"scope": "s", "records": []},
            headers={"Authorization": "Bearer test-key"},
        )
        assert resp.status_code == 403

    def test_ingest_fails_closed_when_api_key_unset(self, monkeypatch):
        monkeypatch.delenv("WEFT_API_KEY", raising=False)
        monkeypatch.setenv("WEFT_KNOWN_SCOPES", "s")
        client = TestClient(mcp_app)
        resp = client.post(
            "/mcp/ingest",
            json={"scope": "s", "records": []},
        )
        assert resp.status_code == 401

    def test_recall_rejects_missing_api_key(self, monkeypatch):
        monkeypatch.setenv("WEFT_API_KEY", "test-key")
        monkeypatch.setenv("WEFT_KNOWN_SCOPES", "s")
        client = TestClient(mcp_app)
        resp = client.post("/mcp/recall", json={"scope": "s", "query": "foo", "limit": 5})
        assert resp.status_code == 401

    def test_recall_with_api_key(self, monkeypatch):
        monkeypatch.setenv("WEFT_API_KEY", "test-key")
        monkeypatch.setenv("WEFT_KNOWN_SCOPES", "s")
        client = TestClient(mcp_app)
        resp = client.post(
            "/mcp/recall",
            json={"scope": "s", "query": "foo", "limit": 5},
            headers={"Authorization": "Bearer test-key"},
        )
        assert resp.status_code == 200

    def test_status_rejects_missing_api_key(self, monkeypatch):
        monkeypatch.setenv("WEFT_API_KEY", "test-key")
        monkeypatch.setenv("WEFT_KNOWN_SCOPES", "s")
        client = TestClient(mcp_app)
        resp = client.post("/mcp/status", json={"scope": "s"})
        assert resp.status_code == 401

    def test_status_with_api_key(self, monkeypatch):
        monkeypatch.setenv("WEFT_API_KEY", "test-key")
        monkeypatch.setenv("WEFT_KNOWN_SCOPES", "s")
        client = TestClient(mcp_app)
        resp = client.post(
            "/mcp/status",
            json={"scope": "s"},
            headers={"Authorization": "Bearer test-key"},
        )
        assert resp.status_code == 200

    def test_call_rejects_missing_api_key(self, monkeypatch):
        monkeypatch.setenv("WEFT_API_KEY", "test-key")
        monkeypatch.setenv("WEFT_KNOWN_SCOPES", "s")
        client = TestClient(mcp_app)
        resp = client.post("/mcp/call", json={"tool": "recall", "arguments": {"scope": "s", "query": "foo", "limit": 5}})
        assert resp.status_code == 401

    def test_call_with_api_key(self, monkeypatch):
        monkeypatch.setenv("WEFT_API_KEY", "test-key")
        monkeypatch.setenv("WEFT_KNOWN_SCOPES", "s")
        client = TestClient(mcp_app)
        resp = client.post(
            "/mcp/call",
            json={"tool": "recall", "arguments": {"scope": "s", "query": "foo", "limit": 5}},
            headers={"Authorization": "Bearer test-key"},
        )
        assert resp.status_code == 200
