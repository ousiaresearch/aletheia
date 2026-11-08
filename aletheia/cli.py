"""Local single-scope CLI composition root for Aletheia.

This is an operator-local tool, not a multi-user service boundary. Each invocation
can read/write only the explicitly named scope; there is no cross-scope grant path.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from aletheia.adapters import default_text_embedder
from aletheia.ingest import Ingestor, Rejection
from aletheia.retrieval import recall
from aletheia.paths import migrate_legacy_cli_store
from aletheia.scope import ScopeContext, ScopeEngine
from aletheia.substrate import SCHEMA_VERSION, Substrate, SubstrateError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aletheia", description="Local, single-scope Aletheia memory CLI")
    parser.add_argument(
        "--db", default=os.path.expanduser("~/.aletheia/aletheia.sqlite3"),
        help="SQLite store path (default: ~/.aletheia/aletheia.sqlite3)",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    ingest = commands.add_parser("ingest", help="ingest a local text, Markdown, PDF, or DOCX file")
    ingest.add_argument("--scope", required=True, help="the one scope this invocation may write")
    ingest.add_argument("--file", required=True, help="path to a local file")
    ingest.add_argument("--author", default="local-user", help="document author label")

    search = commands.add_parser("recall", help="search one explicitly named scope")
    search.add_argument("--scope", required=True, help="the one scope this invocation may read")
    search.add_argument("--query", required=True, help="natural-language search query")
    search.add_argument("--limit", type=int, default=5)
    serve = commands.add_parser("serve", help="start local HTTP service for desktop integration")
    serve.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1)")
    serve.add_argument("--port", type=int, default=8765, help="bind port (default: 8765)")
    rebuild = commands.add_parser("rebuild-fts", help="rebuild one scope's local text index")
    rebuild.add_argument("--scope", required=True)
    
    extract = commands.add_parser("extract", help="extract facts from Observations into Claims")
    extract.add_argument("--scope", required=True)
    extract.add_argument("--limit", type=int, default=100, help="max Observations to process")
    extract.add_argument("--allow-external", action="store_true",
                         help="REQUIRED: send scope text to the external TokenHarbor endpoint "
                              "(credential from TOKENHARBOR_API_KEY); also ALETHEIA_EXTRACT_ALLOW_EXTERNAL=1")
    
    consolidate = commands.add_parser("consolidate", help="deduplicate Claims in a scope")
    consolidate.add_argument("--scope", required=True)

    link_entities = commands.add_parser("link-entities", help="extract entity links from records")
    link_entities.add_argument("--scope", required=True)
    link_entities.add_argument("--limit", type=int, default=100, help="max records to scan")

    temporal = commands.add_parser("temporal", help="query records valid at a given timestamp")
    temporal.add_argument("--scope", required=True)
    temporal.add_argument("--at", type=float, required=True, help="Unix timestamp")

    propose_rule = commands.add_parser("propose-rule", help="propose a new procedural rule")
    propose_rule.add_argument("--scope", required=True)
    propose_rule.add_argument("--rule-id", required=True, help="unique rule identifier")
    propose_rule.add_argument("--name", required=True, help="human-readable rule name")
    propose_rule.add_argument("--description", default="", help="rule description")
    propose_rule.add_argument("--rule-type", default="procedural", choices=["procedural", "prompt_refinement", "retrieval_tweak"])

    active_rules = commands.add_parser("active-rules", help="list active procedural rules in a scope")
    active_rules.add_argument("--scope", required=True)

    reembed = commands.add_parser("reembed", help="embed records in a scope that lack vectors (repairs hybrid recall)")
    reembed.add_argument("--scope", required=True)
    reembed.add_argument("--limit", type=int, default=None, help="max records to embed this run")
    return parser


def _context(scope: str) -> tuple[ScopeEngine, ScopeContext]:
    if not scope.strip():
        raise ValueError("scope must be non-empty")
    engine = ScopeEngine(known_scopes={scope})
    # This local CLI treats the operator's explicit invocation as the authority
    # for exactly one scope; it never accepts cross-scope targets or grants.
    ctx = ScopeContext(granted_scopes=frozenset({scope}), user_authored=True, actor="local-user")
    return engine, ctx


def _run_ingest(args) -> int:
    path = Path(args.file).expanduser()
    data = path.read_bytes()
    engine, ctx = _context(args.scope)
    embedder = default_text_embedder()  # loads/verifies locally; no cloud or lexical fallback
    sub = None
    try:
        sub = Substrate(args.db, engine)
        result = Ingestor(sub, embedder=embedder).ingest_document(
            path.name, data, ctx, args.scope, args.author
        )
        if isinstance(result, Rejection):
            print(json.dumps({"status": "rejected", "reason": result.reason, "name": result.name}))
            return 2
        if not sub.vector_has_record(result, args.scope, ctx, model_id=embedder.model_id):
            raise RuntimeError("ingestion completed without a vector for the adopted model")
        print(json.dumps({
            "status": "ingested", "record_id": result, "scope": args.scope,
            "vector_indexed": True, "model_id": embedder.model_id,
        }))
        return 0
    finally:
        if sub is not None:
            sub.close()


def _run_recall(args) -> int:
    if args.limit < 1 or args.limit > 100:
        raise ValueError("limit must be between 1 and 100")
    db_path = Path(args.db).expanduser()
    if not db_path.is_file():
        raise FileNotFoundError(f"Aletheia database not found: {db_path}")
    embedder = default_text_embedder()  # hybrid recall requires the local query embedder
    engine, ctx = _context(args.scope)
    sub = None
    try:
        sub = Substrate(db_path, engine)
        items, receipt = recall(sub, args.query, ctx, limit=args.limit, mode="hybrid", embedder=embedder)
        print(json.dumps({
            "mode": receipt.mode,
            "scope_consulted": receipt.scope_consulted,
            "selected": receipt.selected,
            "tier": receipt.tier,
            "items": [
                {"record_id": item.record_id, "scope": item.scope, "text": item.text,
                 "tier": item.tier, "derived": item.derived}
                for item in items
            ],
        }, ensure_ascii=False))
        return 0
    finally:
        if sub is not None:
            sub.close()


def _run_rebuild_fts(args) -> int:
    db_path = Path(args.db).expanduser()
    if not db_path.is_file():
        raise FileNotFoundError(f"Aletheia database not found: {db_path}")
    # Inspect without initializing or migrating an unrelated SQLite file. Old
    # schema versions need a separate migration workflow, not this command.
    with closing(sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)) as check:
        version = check.execute("PRAGMA user_version").fetchone()[0]
        objects = {row[0] for row in check.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        if version != SCHEMA_VERSION or not {"records", "fts", "meta", "journal", "jobs"} <= objects:
            raise ValueError("not a supported existing Aletheia store; refusing maintenance")
        if check.execute("SELECT value FROM meta WHERE key='store_version'").fetchone() is None:
            raise ValueError("not a supported existing Aletheia store; refusing maintenance")
    engine, ctx = _context(args.scope)
    sub = Substrate(db_path, engine)
    try:
        sub.rebuild_fts(args.scope, ctx)
        print(json.dumps({"status": "rebuilt", "scope": args.scope, "index": "fts"}))
        return 0
    finally:
        sub.close()


def _prepare_serve_env(args) -> None:
    """Make ``--db`` effective for the server process and print fail-closed notices.

    The server reads ALETHEIA_DB_PATH. An already-set variable wins (warned if it
    differs). Never prints secret values.
    """
    env_db = os.environ.get("ALETHEIA_DB_PATH")
    if env_db is None:
        os.environ["ALETHEIA_DB_PATH"] = args.db
    elif (args.db != os.path.expanduser("~/.aletheia/aletheia.sqlite3")
          and os.path.abspath(os.path.expanduser(env_db)) != os.path.abspath(os.path.expanduser(args.db))):
        print(f"aletheia: warning: ALETHEIA_DB_PATH is set and differs from --db; the server will use ALETHEIA_DB_PATH ({env_db})",
              file=sys.stderr)
    for name in ("ALETHEIA_API_KEY", "ALETHEIA_KNOWN_SCOPES"):
        if not os.environ.get(name):
            print(f"aletheia: warning: {name} is not set; the server fails closed and will refuse requests until it is set",
                  file=sys.stderr)


def _run_serve(args) -> int:
    import uvicorn
    _prepare_serve_env(args)
    from aletheia.server import app
    print(json.dumps({"status": "serving", "host": args.host, "port": args.port}))
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


def _run_extract(args) -> int:
    from aletheia.extraction import destination_host, extract_facts, external_allowed, write_claims
    if args.allow_external:
        os.environ["ALETHEIA_EXTRACT_ALLOW_EXTERNAL"] = "1"
    if not external_allowed():
        print("aletheia extract sends scope text to an external service and is disabled by default; "
              "pass --allow-external (or set ALETHEIA_EXTRACT_ALLOW_EXTERNAL=1) to proceed",
              file=sys.stderr)
        return 2
    print(f"aletheia extract: sending scope text to external host {destination_host()}", file=sys.stderr)
    db_path = Path(args.db).expanduser()
    if not db_path.is_file():
        raise FileNotFoundError(f"Aletheia database not found: {db_path}")
    engine, ctx = _context(args.scope)
    sub = Substrate(db_path, engine)
    try:
        claims = extract_facts(sub, ctx, args.scope, limit=args.limit)
        # Claims are embeddable records: without vectors they would leave the scope's vector
        # index incomplete and disable hybrid recall (the only CLI recall).
        embedder = default_text_embedder() if claims else None
        written = write_claims(sub, ctx, claims, embedder=embedder)
        vectorized = 0
        if embedder is not None:
            vectorized = sum(
                1 for c in claims
                if sub.vector_has_record(c.record_id, args.scope, ctx, model_id=embedder.model_id))
        print(json.dumps({
            "status": "extracted",
            "scope": args.scope,
            "claims_generated": len(claims),
            "claims_written": written,
            "claims_vectorized": vectorized,
        }))
        return 0
    finally:
        sub.close()


def _run_consolidate(args) -> int:
    from aletheia.extraction import consolidate_scope
    db_path = Path(args.db).expanduser()
    if not db_path.is_file():
        raise FileNotFoundError(f"Aletheia database not found: {db_path}")
    engine, ctx = _context(args.scope)
    sub = Substrate(db_path, engine)
    try:
        result = consolidate_scope(sub, ctx, args.scope)
        print(json.dumps({
            "status": "consolidated",
            "scope": args.scope,
            **result,
        }))
        return 0
    finally:
        sub.close()


def _run_link_entities(args) -> int:
    from aletheia.entity_link import link_entities
    db_path = Path(args.db).expanduser()
    if not db_path.is_file():
        raise FileNotFoundError(f"Aletheia database not found: {db_path}")
    engine, ctx = _context(args.scope)
    sub = Substrate(db_path, engine)
    try:
        result = link_entities(sub, ctx, args.scope, limit=args.limit)
        print(json.dumps({
            "status": "linked",
            "scope": args.scope,
            **result,
        }))
        return 0
    finally:
        sub.close()


def _run_temporal(args) -> int:
    from aletheia.temporal import effective_at
    db_path = Path(args.db).expanduser()
    if not db_path.is_file():
        raise FileNotFoundError(f"Aletheia database not found: {db_path}")
    engine, ctx = _context(args.scope)
    sub = None
    try:
        sub = Substrate(db_path, engine)
        records = effective_at(sub, ctx, args.scope, args.at)
        print(json.dumps({
            "status": "effective_at",
            "scope": args.scope,
            "at": args.at,
            "count": len(records),
            "records": [
                {"record_id": r.record_id, "type": r.record_type, "text": r.payload.get("text", "")}
                for r in records
            ],
        }, ensure_ascii=False))
        return 0
    finally:
        if sub is not None:
            sub.close()


def _run_propose_rule(args) -> int:
    from aletheia.procedural import ProposedRule, propose_rule
    db_path = Path(args.db).expanduser()
    if not db_path.is_file():
        raise FileNotFoundError(f"Aletheia database not found: {db_path}")
    engine, ctx = _context(args.scope)
    sub = None
    try:
        sub = Substrate(db_path, engine)
        rule = ProposedRule(
            rule_id=args.rule_id,
            scope=args.scope,
            name=args.name,
            description=args.description,
            rule_type=args.rule_type,
        )
        record = propose_rule(sub, ctx, rule)
        print(json.dumps({
            "status": "proposed",
            "rule_id": record.record_id,
            "scope": record.scope,
            "name": record.payload["name"],
            "rule_type": record.payload["rule_type"],
        }, ensure_ascii=False))
        return 0
    finally:
        if sub is not None:
            sub.close()


def _run_active_rules(args) -> int:
    from aletheia.procedural import list_active_rules
    db_path = Path(args.db).expanduser()
    if not db_path.is_file():
        raise FileNotFoundError(f"Aletheia database not found: {db_path}")
    engine, ctx = _context(args.scope)
    sub = None
    try:
        sub = Substrate(db_path, engine)
        records = list_active_rules(sub, ctx, args.scope)
        print(json.dumps({
            "status": "active_rules",
            "scope": args.scope,
            "count": len(records),
            "rules": [
                {
                    "rule_id": r.record_id,
                    "name": r.payload.get("name", ""),
                    "description": r.payload.get("description", ""),
                    "rule_type": r.payload.get("rule_type", ""),
                    "promoted_by": r.payload.get("promoted_by"),
                    "promoted_at": r.payload.get("promoted_at"),
                }
                for r in records
            ],
        }, ensure_ascii=False))
        return 0
    finally:
        if sub is not None:
            sub.close()


def _run_reembed(args) -> int:
    db_path = Path(args.db).expanduser()
    if not db_path.is_file():
        raise FileNotFoundError(f"Aletheia database not found: {db_path}")
    engine, ctx = _context(args.scope)
    embedder = default_text_embedder()
    sub = Substrate(db_path, engine)
    try:
        result = sub.reembed_missing(args.scope, ctx, embedder, limit=args.limit)
        complete = sub.vector_index_complete(args.scope, ctx, model_id=embedder.model_id)
        print(json.dumps({"status": "reembedded", "scope": args.scope, "model_id": embedder.model_id,
                          "index_complete": complete, **result}))
        return 0
    finally:
        sub.close()


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    # 2.0.0 renamed the product: bring a pre-rename store across once, so the same command keeps
    # finding the same notes. Moving an existing file never creates an empty one.
    migrate_legacy_cli_store(Path(args.db))
    try:
        if args.command == "ingest":
            return _run_ingest(args)
        if args.command == "rebuild-fts":
            return _run_rebuild_fts(args)
        if args.command == "serve":
            return _run_serve(args)
        if args.command == "extract":
            return _run_extract(args)
        if args.command == "consolidate":
            return _run_consolidate(args)
        if args.command == "link-entities":
            return _run_link_entities(args)
        if args.command == "temporal":
            return _run_temporal(args)
        if args.command == "propose-rule":
            return _run_propose_rule(args)
        if args.command == "active-rules":
            return _run_active_rules(args)
        if args.command == "reembed":
            return _run_reembed(args)
        return _run_recall(args)
    except (OSError, RuntimeError, ValueError, ImportError, sqlite3.Error, SubstrateError) as exc:
        print(f"aletheia: {exc}", file=sys.stderr)
        return 1


def entrypoint() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    entrypoint()
