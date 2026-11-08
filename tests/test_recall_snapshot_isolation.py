from aletheia.ingest import Ingestor
from aletheia.retrieval import recall
from aletheia.scope import ScopeContext, ScopeEngine
from aletheia.substrate import Substrate


class Embedder:
    model_id = 'race/model'

    def embed_document(self, text):
        return [1.0] + [0.0] * 1023

    embed_query = embed_document


def test_recall_snapshot_isolates_mid_query_mutation(tmp_path):
    """Completeness check, candidate selection, and per-record reads must observe
    one consistent WAL snapshot (review G-P3-7). A mutation committed by another
    connection during vector_candidates must not bleed into the recalled text."""
    sub = Substrate(tmp_path / 'db', ScopeEngine(known_scopes={'coast'}))
    other = Substrate(tmp_path / 'db', ScopeEngine(known_scopes={'coast'}))
    ctx = ScopeContext(granted_scopes=frozenset({'coast'}))
    try:
        sid = Ingestor(sub, Embedder()).ingest_document('a.txt', b'beacon tide records', ctx, 'coast', 'operator')
        assert isinstance(sid, str)
        original = sub.vector_candidates

        def interleave(*args, **kwargs):
            rec = other.get(sid, ctx)
            assert rec is not None
            rec.payload = {'text': 'MUTATED tide records'}
            other.put(rec, ctx, overwrite=True)
            return original(*args, **kwargs)

        sub.vector_candidates = interleave
        items, _ = recall(sub, 'beacon', ctx, mode='hybrid', embedder=Embedder())
        assert items
        assert items[0].text == 'beacon tide records'
    finally:
        sub.close()
        other.close()
