"""Review ec112bc: GATE-1 (approval pinned in tracked code), empty-gold crash, model identity per checkpoint.

Tests first. The approved hash must come from tracked code, so editing the gold file and
recomputing its hash cannot make a set approved. Model ids must differ between checkpoints.
"""

import hashlib
import importlib.util
import json
import os

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("recall_gate", os.path.join(REPO, "eval", "recall_gate.py"))
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)

ENTRIES = [{"query": "a", "expected_note": "n1.md", "kind": "keyword"}]
PINNED = gate.entries_sha256(ENTRIES)
REAL_PIN = gate.APPROVED_GOLD_SHA256  # captured before any fixture patches it


@pytest.fixture(autouse=True)
def pin(monkeypatch):
    # the approved hash is a constant in tracked code; tests pin it to the fixture entries
    monkeypatch.setattr(gate, "APPROVED_GOLD_SHA256", PINNED)


def test_forged_hash_is_refused_when_not_pinned():
    edited = [{"query": "zzz", "expected_note": "evil.md", "kind": "keyword"}]
    forged = {"status": "APPROVED", "entries": edited, "approved_sha256": gate.entries_sha256(edited)}
    assert not gate.gold_is_approved(forged)


def test_pinned_approval_still_passes():
    assert gate.gold_is_approved({"status": "APPROVED", "entries": ENTRIES, "approved_sha256": PINNED})


def test_empty_entries_are_never_approved(monkeypatch):
    empty_hash = gate.entries_sha256([])
    monkeypatch.setattr(gate, "APPROVED_GOLD_SHA256", empty_hash)
    assert not gate.gold_is_approved({"status": "APPROVED", "entries": [], "approved_sha256": empty_hash})


def test_pinned_constant_matches_the_approved_gold_set():
    # the constant in tracked code must be the hash of the gold set the user approved
    gold = json.load(open(os.path.join(REPO, "eval", "gold-set-draft.json"), encoding="utf-8"))
    assert REAL_PIN == gate.entries_sha256(gold["entries"])


def test_bge_checkpoints_have_distinct_model_ids():
    from aletheia.adapters import BgeM3Embedder
    a = BgeM3Embedder("/x/models--mlx-community--bge-m3-mlx-4bit/snapshots/0a93c12b0684953bdaed14865dc270bb1894a749")
    b = BgeM3Embedder("/x/models--mlx-community--bge-m3-mlx-8bit/snapshots/7eca4a1c6ea1a0c5efc37598b369012f3985910f")
    assert a.model_id != b.model_id
    assert "bge-m3-mlx-4bit" in a.model_id
