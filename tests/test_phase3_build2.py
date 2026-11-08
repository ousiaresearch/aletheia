"""Phase 3 build pass 2: real adapters, tiers, source existence, media crash path.

Integration tests run the real local models. They skip cleanly if the model
files are absent, and they are the evidence for the adapters, not stand-ins.
"""

import os
import shutil
from pathlib import Path

import pytest

from aletheia.ingest import Ingestor, Rejection
from aletheia.retrieval import recall
from aletheia.scope import ScopeContext, ScopeEngine
from aletheia.substrate import Record, ScopeViolation, Substrate

REPO = Path(__file__).resolve().parents[1]
VLM_DIR = REPO / "models" / "qwen3.5-2b-8bit-mlx"
WHISPER = REPO / "models" / "whisper" / "ggml-large-v3-turbo-q5_0.bin"
SMOKE_PNG = REPO / "models" / "smoke.png"
SMOKE_WAV = REPO / "models" / "smoke.wav"

KNOWN = {"acs-project", "memory-providers"}
MARKERS = {"acs-project": {"ACS", "Listening Commonwealth"}}
USER = ScopeContext(granted_scopes=frozenset(KNOWN), user_authored=True, actor="johann")
MEM_AGENT = ScopeContext(granted_scopes=frozenset({"memory-providers"}), actor="arien")


@pytest.fixture
def sub(tmp_path):
    s = Substrate(tmp_path / "aletheia.db", scope_engine=ScopeEngine(known_scopes=KNOWN, markers=MARKERS))
    yield s
    s.close()


@pytest.fixture
def ing(sub):
    return Ingestor(sub)


# ---- Retrieval tiers (spec D3-11) ------------------------------------------

def test_tier_l1_when_query_term_is_past_the_abstract(sub, ing):
    long_text = ("filler sentence about nothing. " * 30) + "The quartz anchor sits near the end."
    ing.ingest_turn(long_text, "user", USER, "memory-providers", "c1")
    items, receipt = recall(sub, "quartz anchor", MEM_AGENT, mode="lexical")
    assert items and items[0].tier == "L1"
    assert "quartz" in items[0].text
    assert receipt.tier == "L1"


def test_tier_l2_when_term_is_past_two_thousand_chars(sub, ing):
    text = ("padding words here. " * 200) + "The obsidian lighthouse waits at the very end."
    ing.ingest_turn(text, "user", USER, "memory-providers", "c1")
    items, receipt = recall(sub, "obsidian lighthouse", MEM_AGENT, mode="lexical")
    assert items and items[0].tier == "L2"
    assert "obsidian" in items[0].text
    assert receipt.tier == "L2"


def test_tier_l0_when_abstract_already_matches(sub, ing):
    ing.ingest_turn("Honcho uses hybrid recall.", "user", USER, "memory-providers", "c1")
    items, receipt = recall(sub, "honcho", MEM_AGENT, mode="lexical")
    assert items[0].tier == "L0"
    assert receipt.tier == "L0"


# ---- Source existence (spec §4, closes Phase 2 residual R-6) ----------------

def test_observation_citing_missing_source_refused(sub):
    with pytest.raises(ScopeViolation):
        sub.put(Record(record_id="o-orphan", record_type="Observation", scope="memory-providers",
                       payload={"text": "x"}, attribution="document", source_ids=["no-such-source"]), USER)


def test_observation_citing_existing_source_allowed(sub, ing):
    sid = ing.ingest_turn("source text", "user", USER, "memory-providers", "c1")
    sub.put(Record(record_id="o-ok", record_type="Observation", scope="memory-providers",
                   payload={"text": "x"}, attribution="document", source_ids=[sid]), USER)
    assert sub.get("o-ok", USER) is not None


# ---- Media crash path (spec D3-7) -----------------------------------------

def test_media_derive_killed_mid_run_resumes_once(sub, ing):
    calls = {"n": 0}

    def flaky(_data):
        calls["n"] += 1
        if calls["n"] == 1:
            raise KeyboardInterrupt
        return "A lighthouse at dusk."

    with pytest.raises(KeyboardInterrupt):
        ing.ingest_media("p.png", b"\x89PNG-a", "image", USER, "memory-providers", describer=flaky)
    assert not [r for r in sub.all_records(USER) if r.attribution == "media-derived"]
    sid = ing.ingest_media("p.png", b"\x89PNG-a", "image", USER, "memory-providers", describer=flaky)
    assert isinstance(sid, str)
    assert len([r for r in sub.all_records(USER) if r.attribution == "media-derived"]) == 1


def test_media_without_adapter_is_a_named_rejection(sub, ing):
    result = ing.ingest_media("p.png", b"\x89PNG-b", "image", USER, "memory-providers")
    assert isinstance(result, Rejection)
    assert result.reason == "no_adapter_configured"


# ---- Real adapters (integration: the evidence, not a stand-in) --------------

needs_vlm = pytest.mark.skipif(not VLM_DIR.exists(), reason="Qwen3.5-2B model not on disk")
needs_whisper = pytest.mark.skipif(not WHISPER.exists(), reason="whisper model not on disk")


@needs_vlm
def test_real_vlm_reads_text_in_smoke_image(sub, ing):
    from aletheia.adapters import QwenVLM
    adapter = QwenVLM(VLM_DIR)
    sid = ing.ingest_media("smoke.png", SMOKE_PNG.read_bytes(), "image", USER, "memory-providers",
                           describer=adapter)
    rec = sub.get(sid, USER)
    assert "aletheia memory test 42" in rec.payload["text"].lower()
    assert rec.provenance["machine_derived"] is True
    assert rec.provenance["model"].startswith("Qwen3.5-2B")


@needs_whisper
def test_real_whisper_transcribes_smoke_audio(sub, ing):
    from aletheia.adapters import WhisperTranscriber
    adapter = WhisperTranscriber(WHISPER)
    sid = ing.ingest_media("smoke.wav", SMOKE_WAV.read_bytes(), "audio", USER, "memory-providers",
                           transcriber=adapter)
    text = sub.get(sid, USER).payload["text"].lower()
    assert "aletheia" in text and "thread" in text
    assert sub.get(sid, USER).provenance["model"].startswith("whisper")
