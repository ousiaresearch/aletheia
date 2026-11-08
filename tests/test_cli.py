"""CLI composition root tests: explicit single-scope local ingestion and recall."""

import json

from aletheia import cli


class FakeEmbedder:
    model_id = "test/model@v1"

    def embed_document(self, text):
        return [1.0] + [0.0] * 1023

    def embed_query(self, text):
        return [1.0] + [0.0] * 1023


def test_ingest_and_recall_are_local_single_scope_and_vectorized(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "default_text_embedder", lambda: FakeEmbedder())
    db = tmp_path / "aletheia.db"
    note = tmp_path / "note.md"
    note.write_text("The lighthouse keepers record the tides each night.", encoding="utf-8")

    assert cli.main(["--db", str(db), "ingest", "--scope", "coast", "--file", str(note)]) == 0
    ingested = json.loads(capsys.readouterr().out)
    assert ingested["status"] == "ingested"
    assert ingested["scope"] == "coast"
    assert ingested["vector_indexed"] is True
    from aletheia.scope import ScopeContext, ScopeEngine
    from aletheia.substrate import Substrate
    check = Substrate(db, ScopeEngine(known_scopes={"coast"}))
    try:
        assert check.vector_has_record(
            ingested["record_id"], "coast", ScopeContext(granted_scopes=frozenset({"coast"})),
            model_id=ingested["model_id"],
        )
    finally:
        check.close()

    assert cli.main(["--db", str(db), "recall", "--scope", "coast", "--query", "who records tides?"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["mode"] == "hybrid"
    assert result["items"]
    assert result["items"][0]["scope"] == "coast"


def test_cli_rejects_missing_model_without_silent_fallback(tmp_path, monkeypatch, capsys):
    def missing():
        raise RuntimeError("embedding model not present")

    monkeypatch.setattr(cli, "default_text_embedder", missing)
    note = tmp_path / "note.txt"
    note.write_text("Local memory text", encoding="utf-8")
    db = tmp_path / "aletheia.db"
    code = cli.main(["--db", str(db), "ingest", "--scope", "local", "--file", str(note)])
    assert code != 0
    assert "embedding model not present" in capsys.readouterr().err
    assert not db.exists()


def test_cli_scope_isolation_has_positive_control(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "default_text_embedder", lambda: FakeEmbedder())
    db = tmp_path / "aletheia.db"
    note = tmp_path / "secret.txt"
    secret = "Only the coastal archivist records the rare blue tide."
    note.write_text(secret, encoding="utf-8")
    assert cli.main(["--db", str(db), "ingest", "--scope", "coast", "--file", str(note)]) == 0
    ingested = json.loads(capsys.readouterr().out)
    assert ingested["status"] == "ingested"

    assert cli.main(["--db", str(db), "recall", "--scope", "coast", "--query", "blue tide"]) == 0
    in_scope = json.loads(capsys.readouterr().out)
    assert in_scope["items"]
    assert secret in in_scope["items"][0]["text"]

    code = cli.main(["--db", str(db), "recall", "--scope", "inland", "--query", "blue tide"])
    result = json.loads(capsys.readouterr().out)
    assert code == 0
    assert result["items"] == []
    assert result["scope_consulted"] == ["inland"]
    assert secret not in json.dumps(result)


def test_cli_refuses_hybrid_when_model_index_does_not_match(tmp_path, monkeypatch, capsys):
    db = tmp_path / "aletheia.db"
    note = tmp_path / "coast.md"
    note.write_text("The lighthouse keeps a record of rare tides.", encoding="utf-8")
    embedders = [FakeEmbedder(), FakeEmbedder()]
    embedders[1].model_id = "test/model@v2"
    monkeypatch.setattr(cli, "default_text_embedder", lambda: embedders.pop(0))
    assert cli.main(["--db", str(db), "ingest", "--scope", "coast", "--file", str(note)]) == 0
    capsys.readouterr()

    code = cli.main(["--db", str(db), "recall", "--scope", "coast", "--query", "what are rare tides?"])
    assert code != 0
    assert "vector" in capsys.readouterr().err.lower()


def test_cli_rejects_empty_document_instead_of_claiming_vectors(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "default_text_embedder", lambda: FakeEmbedder())
    db = tmp_path / "aletheia.db"
    note = tmp_path / "empty.md"
    note.write_text("  \n\n", encoding="utf-8")

    code = cli.main(["--db", str(db), "ingest", "--scope", "coast", "--file", str(note)])
    result = json.loads(capsys.readouterr().out)
    assert code == 2
    assert result["status"] == "rejected"
    assert result["reason"] == "empty_document"
    from aletheia.scope import ScopeEngine
    from aletheia.substrate import Substrate
    sub = Substrate(db, ScopeEngine(known_scopes={"coast"}))
    try:
        assert sub._conn.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 0
        assert sub._conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    finally:
        sub.close()


def test_cli_rejects_wrong_query_vector_dimension(tmp_path, monkeypatch, capsys):
    db = tmp_path / "aletheia.db"
    note = tmp_path / "coast.md"
    note.write_text("The lighthouse keeps a record of rare tides.", encoding="utf-8")

    class WrongQueryDimension(FakeEmbedder):
        def embed_query(self, text):
            return [1.0, 0.0]

    monkeypatch.setattr(cli, "default_text_embedder", lambda: FakeEmbedder())
    assert cli.main(["--db", str(db), "ingest", "--scope", "coast", "--file", str(note)]) == 0
    capsys.readouterr()
    monkeypatch.setattr(cli, "default_text_embedder", lambda: WrongQueryDimension())

    code = cli.main(["--db", str(db), "recall", "--scope", "coast", "--query", "rare tide"])
    assert code != 0
    assert "dimension" in capsys.readouterr().err.lower()


def test_recall_refuses_nonexistent_store_instead_of_creating_empty_database(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "default_text_embedder", lambda: FakeEmbedder())
    db = tmp_path / "mistyped" / "missing.db"
    code = cli.main(["--db", str(db), "recall", "--scope", "coast", "--query", "tides"])
    assert code != 0
    assert not db.exists()
    assert not db.parent.exists()


def test_malformed_database_is_a_clean_nonzero_cli_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "default_text_embedder", lambda: FakeEmbedder())
    db = tmp_path / "bad.db"
    db.write_bytes(b"not sqlite")
    code = cli.main(["--db", str(db), "recall", "--scope", "coast", "--query", "tides"])
    assert code != 0
    error = capsys.readouterr().err
    assert "aletheia:" in error
    assert "Traceback" not in error


def test_scope_isolation_recall_succeeds_with_empty_other_scope(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "default_text_embedder", lambda: FakeEmbedder())
    db = tmp_path / "aletheia.db"
    note = tmp_path / "coast.md"
    note.write_text("coast phrase retained", encoding="utf-8")
    assert cli.main(["--db", str(db), "ingest", "--scope", "coast", "--file", str(note)]) == 0
    capsys.readouterr()
    assert cli.main(["--db", str(db), "recall", "--scope", "inland", "--query", "coast phrase"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["items"] == []


def test_embedder_failure_does_not_leave_partial_ingestion(tmp_path, monkeypatch, capsys):
    class FailingEmbedder(FakeEmbedder):
        def embed_document(self, text):
            raise RuntimeError("model load failed")

    monkeypatch.setattr(cli, "default_text_embedder", lambda: FailingEmbedder())
    db = tmp_path / "aletheia.db"
    note = tmp_path / "failure.md"
    note.write_text("A non-empty document that should not be stored half-processed.", encoding="utf-8")
    assert cli.main(["--db", str(db), "ingest", "--scope", "coast", "--file", str(note)]) != 0
    assert "model load failed" in capsys.readouterr().err

    from aletheia.scope import ScopeEngine
    from aletheia.substrate import Substrate
    sub = Substrate(db, ScopeEngine(known_scopes={"coast"}))
    try:
        assert sub._conn.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 0
        assert sub._conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    finally:
        sub.close()
