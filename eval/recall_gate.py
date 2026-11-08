"""Recall gate (spec D3-1): measured recall@1 and recall@5 on a gold set.

Ingests the Arien vault notes into a fresh store (scope acs-project), then scores each
gold entry in lexical, vector, and hybrid modes. A hit means the expected note is among
the top-k returned records' documents.

Thresholds are user decisions (see the ADR): hybrid recall@5 must reach RECALL5_FLOOR and
must not fall below lexical recall@5 for vector retrieval to be enabled by default.
The output records the gold-set status; a DRAFT gold set is not acceptance evidence.
"""

import glob
import hashlib
import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from aletheia.adapters import HarrierEmbedder  # noqa: E402
from aletheia.ingest import Ingestor  # noqa: E402
from aletheia.retrieval import recall  # noqa: E402
from aletheia.scope import ScopeContext, ScopeEngine  # noqa: E402
from aletheia.substrate import Substrate  # noqa: E402

VAULT = Path(os.path.expanduser("~/Documents/Obsidian Vault/Arien"))
SCRATCH = Path(os.path.expanduser("~/.hermes/profiles/arien/cache/scratch"))
MODEL = None  # Harrier path resolved only when EMBEDDER == "harrier" (its cache was removed in cleanup)
SCOPE = "acs-project"
RECALL5_FLOOR = 0.70  # user decision 2026-10-27: floor lowered from 0.90 for alpha (hybrid must reach this and lexical)
K = 5
USER = ScopeContext(granted_scopes=frozenset({SCOPE}), user_authored=True, actor="recall-gate")


class CachedEmbedder:
    """Caches vectors on disk by sha256(text). Only the harness uses it; results are unchanged."""

    def __init__(self, inner, cache_path):
        import hashlib, pickle
        self._inner = inner
        self._path = Path(cache_path)
        self._hash = lambda t: hashlib.sha256(t.encode("utf-8")).hexdigest()
        self._cache = pickle.load(open(self._path, "rb")) if self._path.exists() else {}
        self.dim = inner.dim
        self.model_id = inner.model_id
        self._dirty = False

    def _get(self, kind, text):
        key = (kind, self._hash(text))
        if key not in self._cache:
            fn = self._inner.embed_query if kind == "q" else self._inner.embed_document
            self._cache[key] = fn(text)
            self._dirty = True
        return self._cache[key]

    def embed_document(self, text):
        return self._get("d", text)

    def embed_query(self, text):
        return self._get("q", text)

    def save(self):
        import pickle
        if self._dirty:
            pickle.dump(self._cache, open(self._path, "wb"))


EMBEDDER = "bge"  # adopted default: BAAI/bge-m3 (MIT)


def entries_sha256(entries):
    """Hash of the gold entries exactly as the user approved them (canonical JSON)."""
    return hashlib.sha256(json.dumps(entries, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


# Review ec112bc GATE-1: the approved hash is pinned in tracked code. Editing the gold file and
# recomputing its own hash must not make a set approved; changing this constant needs a reviewed commit.
APPROVED_GOLD_SHA256 = "8d245fab980ee4aeef7424ea16c55340a60d21bb03d59ba6abe54236644d86c7"  # gold v0.4, approved by the user 2026-10-26


def gold_is_approved(gold: dict) -> bool:
    """Approval is exact, content-bound, and pinned in tracked code (review 717b85a F6, ec112bc GATE-1).

    Status must be exactly "APPROVED"; entries must be non-empty; the entries' hash must equal the
    pinned constant AND the hash recorded in the file.
    """
    entries = gold.get("entries") or []
    if not entries or gold.get("status") != "APPROVED":
        return False
    digest = entries_sha256(entries)
    return digest == APPROVED_GOLD_SHA256 and gold.get("approved_sha256") == digest


def make_embedder():
    if EMBEDDER in ("bge", "bge-fp16", "bge-8bit"):
        from aletheia.adapters import BgeM3Embedder
        repo = {"bge": "bge-m3-mlx-4bit", "bge-fp16": "bge-m3-mlx-fp16", "bge-8bit": "bge-m3-mlx-8bit"}[EMBEDDER]
        model = glob.glob(os.path.expanduser(
            f"~/.cache/huggingface/hub/models--mlx-community--{repo}/snapshots/*"))[0]
        return BgeM3Embedder(model)
    if EMBEDDER == "qwen3":
        from aletheia.adapters import Qwen3Embedder
        model = glob.glob(os.path.expanduser(
            "~/.cache/huggingface/hub/models--Qwen--Qwen3-Embedding-0.6B/snapshots/*"))[0]
        return Qwen3Embedder(model)
    if EMBEDDER == "harrier":
        model = glob.glob(os.path.expanduser(
            "~/.cache/huggingface/hub/models--microsoft--harrier-oss-v1-0.6b/snapshots/*"))[0]
        return HarrierEmbedder(model)
    raise ValueError(EMBEDDER)


def cache_path_for(name):
    # one cache per model: vectors from different models must never mix
    return SCRATCH / f"embed_cache-{name}.pkl"


def _doc_name(sub, rid):
    rec = sub.get(rid, USER)
    if rec is None or not isinstance(rec.provenance, dict):
        return None
    return rec.provenance.get("document")


def main(gold_path: str, out_path: str, doc_vectors: bool = False, centroid: bool = False):
    gold = json.load(open(gold_path, encoding="utf-8"))
    entries = gold["entries"]
    db = SCRATCH / "recall_gate.db"
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(str(db) + suffix):
            os.remove(str(db) + suffix)
    sub = Substrate(db, scope_engine=ScopeEngine(known_scopes={SCOPE}, markers={}))
    emb = CachedEmbedder(make_embedder(), cache_path_for(EMBEDDER))
    ing = Ingestor(sub, embedder=emb, doc_vectors=doc_vectors, doc_centroid=centroid)

    t0 = time.time()
    files = sorted(VAULT.rglob("*.md"))
    ingested, rejected = 0, []
    for f in files:
        rel = f.relative_to(VAULT).as_posix()
        res = ing.ingest_document(rel, f.read_bytes(), USER, SCOPE, "vault")
        if isinstance(res, str):
            ingested += 1
        else:
            rejected.append((rel, getattr(res, "reason", "?")))
    index_seconds = round(time.time() - t0, 1)

    vault_names = {f.relative_to(VAULT).as_posix() for f in files}
    missing = [e["expected_note"] for e in entries if e["expected_note"] not in vault_names]

    modes = ("lexical", "vector", "hybrid")
    per_entry = []
    hits = {m: {1: 0, K: 0} for m in modes}
    t1 = time.time()
    for e in entries:
        row = {"query": e["query"], "expected": e["expected_note"], "kind": e.get("kind", "keyword")}
        for m in modes:
            items, _ = recall(sub, e["query"], USER, limit=K, mode=m, embedder=emb)
            names = [_doc_name(sub, i.record_id) for i in items]
            rank = next((idx + 1 for idx, n in enumerate(names) if n == e["expected_note"]), None)
            row[m] = rank
            if m == "vector":
                row["vector_top"] = names
            if rank is not None and rank <= 1:
                hits[m][1] += 1
            if rank is not None and rank <= K:
                hits[m][K] += 1
        per_entry.append(row)
    query_seconds = round(time.time() - t1, 1)
    emb.save()

    n = len(entries)
    recall_at = {m: {"recall@1": round(hits[m][1] / n, 3), "recall@5": round(hits[m][K] / n, 3)}
                 for m in modes}
    approved = gold_is_approved(gold)
    passes = (recall_at["hybrid"]["recall@5"] >= RECALL5_FLOOR
              and recall_at["hybrid"]["recall@5"] >= recall_at["lexical"]["recall@5"])
    # R-B6-3: a draft gold set is never a decision. Only an approved set can enable vector.
    enable_vector = passes if approved else None  # None = not decided
    by_kind = {}
    for kind in sorted({r["kind"] for r in per_entry}):
        rows = [r for r in per_entry if r["kind"] == kind]
        by_kind[kind] = {m: {"recall@5": round(sum(1 for r in rows if r[m] is not None and r[m] <= K) / len(rows), 3),
                             "n": len(rows)} for m in modes}
    result = {
        "variant": "centroid" if centroid else ("doc_vectors" if doc_vectors else "baseline"),
        "recall_by_kind": by_kind,
        "gold_status": gold.get("status", ""),
        "gold_entries": n,
        "notes_ingested": ingested,
        "notes_rejected": rejected,
        "gold_expected_notes_missing": missing,
        "index_seconds": index_seconds,
        "query_seconds": query_seconds,
        "recall": recall_at,
        "recall5_floor": RECALL5_FLOOR,
        "gate_passes_floor": passes,
        "vector_default_on": enable_vector,
        "decision": "decided" if approved else "not decided: gold set is not approved",
        "per_entry": per_entry,
    }
    json.dump(result, open(out_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(json.dumps({k: v for k, v in result.items() if k != "per_entry"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    args = sys.argv[1:]
    if "qwen3" in args:
        EMBEDDER = "qwen3"  # noqa: F811 (module-level switch for this run)
    if "bge-fp16" in args:
        EMBEDDER = "bge-fp16"  # noqa: F811
    elif "bge-8bit" in args:
        EMBEDDER = "bge-8bit"  # noqa: F811
    elif "bge" in args:
        EMBEDDER = "bge"  # noqa: F811
    doc_vec = "doc-vectors" in args
    centroid = "centroid" in args
    out = {"centroid": "recall_gate_results-centroid.json", "doc-vectors": "recall_gate_results-docvectors.json"}.get(
        "centroid" if centroid else ("doc-vectors" if doc_vec else ""), "recall_gate_results.json")
    gold = next((a for a in args if a.endswith(".json")), str(REPO / "eval" / "gold-set-draft.json"))
    if EMBEDDER in ("qwen3", "bge", "bge-fp16", "bge-8bit"):
        out = out.replace(".json", f"-{EMBEDDER}.json")
    main(gold, str(REPO / "eval" / out), doc_vectors=doc_vec, centroid=centroid)
