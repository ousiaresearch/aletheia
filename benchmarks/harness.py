"""Benchmark harness for Aletheia memory recall.

Loads gold datasets from benchmarks/datasets/, runs recall on a fresh substrate
per case, and reports recall@k, MRR, abstention accuracy, and latency.
"""

from __future__ import annotations

import json
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from aletheia.retrieval import recall
from aletheia.scope import ScopeContext, ScopeEngine
from aletheia.substrate import Record, Substrate

BENCH_DIR = Path(__file__).resolve().parents[1] / "benchmarks"
DATASET_DIR = BENCH_DIR / "datasets"
KNOWN = frozenset({"locoomo", "longmem", "beam"})


@dataclass(frozen=True)
class Case:
    case_id: str
    query: str
    gold_record_ids: list[str]
    gold_text_tokens: list[str]
    abstain: bool
    setup_records: list[dict]


@dataclass(frozen=True)
class Result:
    case: Case
    case_id: str
    passed: bool
    recall_at_5: bool
    recall_at_10: bool
    mrr: float
    abstention_correct: bool | None
    latency_ms: float
    selected_ids: list[str]
    failure_reason: str | None = None


#: Dataset name -> file stem. The scope inside a set is the short name; the file kept its longer one
#: for longmem, which made `--datasets longmem` fail on a file that was present all along.
DATASET_FILES = {"locoomo": "locoomo-v1", "longmem": "longmemeval-s-v1", "beam": "beam-sm-v1"}


def _load_dataset(name: str) -> list[Case]:
    path = DATASET_DIR / f"{DATASET_FILES.get(name, f'{name}-v1')}.json"
    if not path.exists():
        raise FileNotFoundError(f"dataset not found: {path}")
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    cases: list[Case] = []
    for item in raw:
        cases.append(
            Case(
                case_id=item["id"],
                query=item["query"],
                gold_record_ids=item["gold_record_ids"],
                gold_text_tokens=item.get("gold_text_tokens", []),
                abstain=item.get("abstain", False),
                setup_records=item["setup_records"],
            )
        )
    return cases


def _map_record_type(raw_type: str) -> tuple[str, str, str]:
    """Map dataset record type to valid Aletheia type, attribution, and status."""
    mapping = {
        "Observation": ("Source", "document", "stated"),
        "Document": ("Source", "document", "stated"),
        "Claim": ("Claim", "agent", "derived"),
        "Correction": ("Correction", "user", "stated"),
        "Decision": ("Decision", "user", "stated"),
        "Commitment": ("Commitment", "user", "stated"),
        "Verification": ("Verification", "verifier", "stated"),
    }
    return mapping.get(raw_type, ("Source", "document", "stated"))


def _insert_records(sub: Substrate, ctx: ScopeContext, records: list[dict]) -> None:
    for rec in records:
        scope = rec.get("scope", "default")
        record_id = rec["record_id"]
        raw_type = rec.get("record_type", "Observation")
        record_type, attribution, status = _map_record_type(raw_type)
        text = rec.get("text", "")
        payload = {"text": text} if text else {}
        provenance: dict = {}
        if "session" in rec:
            provenance["session"] = rec["session"]
        record = Record(
            record_id=record_id,
            record_type=record_type,
            scope=scope,
            payload=payload,
            epistemic_status=status,
            attribution=attribution,
            provenance=provenance,
        )
        sub.put(record, ctx)


def _run_case(case: Case, embedder=None, vectors: dict[str, list[float]] | None = None) -> Result:
    engine = ScopeEngine(known_scopes=KNOWN)
    with tempfile.TemporaryDirectory(prefix="aletheia-bench-") as tmp:
        db_path = Path(tmp) / f"{case.case_id}.db"
        sub = Substrate(db_path, engine)
        ctx = ScopeContext(granted_scopes=KNOWN, user_authored=True)

        try:
            _insert_records(sub, ctx, case.setup_records)
            if vectors:
                # The hybrid vector arm needs document vectors: without them a "hybrid" run embeds
                # only the query and fuses against an empty index, which measures nothing new.
                scopes = {rec["record_id"]: rec.get("scope", "default") for rec in case.setup_records}
                model_id = getattr(embedder, "model_id", "benchmark")
                for record_id, vector in vectors.items():
                    scope = scopes.get(record_id)
                    if scope is None or vector is None:
                        continue
                    try:
                        sub.vector_put(record_id, scope, vector, ctx=ctx, model_id=model_id)
                    except Exception:
                        pass
            for scope in ["locoomo", "longmem", "beam"]:
                try:
                    sub.rebuild_fts(scope, ctx)
                except Exception:
                    pass

            start = time.perf_counter()
            items, receipt = recall(
                sub,
                case.query,
                ctx,
                limit=10,
                mode="hybrid" if embedder is not None else "lexical",
                embedder=embedder,
            )
            latency_ms = (time.perf_counter() - start) * 1000.0

            selected_ids = [item.record_id for item in items]
            gold_set = set(case.gold_record_ids)
            hit_5 = bool(gold_set & set(selected_ids[:5]))
            hit_10 = bool(gold_set & set(selected_ids[:10]))

            mrr = 0.0
            for rank, rid in enumerate(selected_ids, start=1):
                if rid in gold_set:
                    mrr = 1.0 / rank
                    break

            abstention_correct = None
            if case.abstain:
                abstention_correct = len(selected_ids) == 0
            else:
                abstention_correct = len(selected_ids) > 0

            text_match = True
            if not case.abstain and gold_set and case.gold_text_tokens:
                selected_texts = []
                for rid in selected_ids[:5]:
                    rec = sub.get(rid, ctx)
                    if rec:
                        selected_texts.append(
                            (rec.payload.get("text", "") if isinstance(rec.payload, dict) else str(rec.payload)).lower()
                        )
                combined = " ".join(selected_texts)
                for token in case.gold_text_tokens:
                    for word in token.lower().split():
                        if word not in combined:
                            text_match = False
                            break
                    if not text_match:
                        break

            if case.abstain:
                passed = bool(abstention_correct)
                failure = None if abstention_correct else "expected abstention but got results"
            else:
                passed = hit_5 and abstention_correct and text_match
                failure = None
                if not hit_5:
                    failure = f"gold {case.gold_record_ids} not in top-5; got {selected_ids[:5]}"
                if abstention_correct is False:
                    failure = (failure + "; " if failure else "") + "expected abstention but got results"
                if not text_match:
                    failure = (failure + "; " if failure else "") + "gold text tokens missing from selected text"

            return Result(
                case=case,
                case_id=case.case_id,
                passed=passed,
                recall_at_5=hit_5,
                recall_at_10=hit_10,
                mrr=mrr,
                abstention_correct=abstention_correct,
                latency_ms=latency_ms,
                selected_ids=selected_ids,
                failure_reason=failure,
            )
        finally:
            sub.close()


def run_dataset(name: str, embedder=None) -> tuple[list[Result], dict]:
    cases = _load_dataset(name)
    results: list[Result] = []
    for case in cases:
        results.append(_run_case(case, embedder=embedder))

    answerable = [r for r in results if not r.case.abstain]
    abstentions = [r for r in results if r.case.abstain]

    passed = sum(1 for r in results if r.passed)
    recall5 = sum(1 for r in answerable if r.recall_at_5)
    recall10 = sum(1 for r in answerable if r.recall_at_10)
    mrr_sum = sum(r.mrr for r in answerable)
    abstent_correct = sum(1 for r in abstentions if r.abstention_correct)
    latencies = [r.latency_ms for r in results]
    latencies_sorted = sorted(latencies)
    p50 = latencies_sorted[len(latencies_sorted) // 2]
    p95 = latencies_sorted[int(len(latencies_sorted) * 0.95)]

    agg = {
        "dataset": name,
        "cases": len(results),
        "answerable": len(answerable),
        "abstentions": len(abstentions),
        "passed": passed,
        "recall_at_5_rate": recall5 / len(answerable) if answerable else 0.0,
        "recall_at_10_rate": recall10 / len(answerable) if answerable else 0.0,
        "mrr": mrr_sum / len(answerable) if answerable else 0.0,
        "abstention_accuracy": abstent_correct / len(abstentions) if abstentions else 0.0,
        "latency_p50_ms": p50,
        "latency_p95_ms": p95,
    }
    return results, agg


def fmt_results_table(results: list[Result], agg: dict) -> str:
    lines = []
    lines.append(f"## {agg['dataset']} — {agg['answerable']} answerable + {agg['abstentions']} abstention cases")
    lines.append("")
    lines.append(
        f"Pass: {agg['passed']}/{agg['cases']}  "
        f"R@5: {agg['recall_at_5_rate']:.3f}  "
        f"R@10: {agg['recall_at_10_rate']:.3f}  "
        f"MRR: {agg['mrr']:.3f}  "
        f"Abstain: {agg['abstention_accuracy']:.0%}  "
        f"p50: {agg['latency_p50_ms']:.1f}ms  p95: {agg['latency_p95_ms']:.1f}ms"
    )
    lines.append("")
    lines.append("| Case | R@5 | R@10 | MRR | Latency | Status |")
    lines.append("|------|-----|------|-----|---------|--------|")
    for r in results:
        status = "PASS" if r.passed else "FAIL"
        lines.append(
            f"| {r.case_id} | {'y' if r.recall_at_5 else 'n'} | {'y' if r.recall_at_10 else 'n'} | {r.mrr:.2f} | {r.latency_ms:.1f}ms | {status} |"
        )
    return "\n".join(lines)
