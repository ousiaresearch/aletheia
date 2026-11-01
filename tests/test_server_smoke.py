"""Smoke tests for Weft HTTP server and MCP server."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error

import pytest

# Skip if FastAPI test client deps are missing
pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from weft.server import app as server_app
from weft.mcp_server import app as mcp_app


class TestServerSmoke:
    def test_health(self):
        client = TestClient(server_app)
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_query_requires_scope(self):
        client = TestClient(server_app)
        resp = client.post("/query", json={"query": "foo"})
        assert resp.status_code == 422

    def test_export_requires_scope(self):
        client = TestClient(server_app)
        resp = client.post("/export", json={})
        assert resp.status_code == 422

    def test_get_record_requires_scope(self):
        client = TestClient(server_app)
        resp = client.get("/record/rec1")
        assert resp.status_code == 422


class TestMCPsmoke:
    def test_health(self):
        client = TestClient(mcp_app)
        resp = client.get("/health")
        assert resp.status_code == 200

    def test_ingest_requires_scope(self):
        client = TestClient(mcp_app)
        resp = client.post("/mcp/ingest", json={})
        assert resp.status_code == 422

    def test_recall_requires_scope(self):
        client = TestClient(mcp_app)
        resp = client.post("/mcp/recall", json={})
        assert resp.status_code == 422

    def test_status_requires_scope(self):
        client = TestClient(mcp_app)
        resp = client.post("/mcp/status", json={})
        assert resp.status_code == 422
