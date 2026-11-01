# Weft Turnkey Deployment

## Docker (recommended)

```bash
# Build and start
docker compose up --build

# The server is now at http://localhost:8765
```

### Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `WEFT_KNOWN_SCOPES` | empty | Comma-separated allowed scopes (empty = any scope) |
| `WEFT_DB_PATH` | `~/.weft/weft.sqlite3` | SQLite database path |

### Health check

```bash
curl http://localhost:8765/health
# {"status":"ok"}
```

### API endpoints

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/mcp/ingest` | Insert records |
| POST | `/mcp/recall` | Hybrid/lexical recall |
| POST | `/mcp/status` | Scope stats |
| POST | `/mcp/call` | Generic MCP tool router |
| GET | `/health` | Health check |

### MCP tool router

`POST /mcp/call` accepts:
```json
{"tool": "recall", "arguments": {"scope": "default", "query": "foo", "limit": 5}}
```

Supported tools: `ingest`, `recall`, `status`, `export`.

## Local install

```bash
pip install "weft-memory[server]"
weft-server --host 0.0.0.0 --port 8765
```

## Scope invariants

- The server enforces deny-by-default scope access.
- `WEFT_KNOWN_SCOPES` limits which scopes are reachable.
- Identity grants no scope; every request must name its scope explicitly.
