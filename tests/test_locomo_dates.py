"""Tests for LoCoMo's session dates reaching the substrate.

The dataset ships a timestamp per session (`session_1_date_time` = "1:56 pm on 8 May, 2023") and this
converter discarded them, which left 321 temporal questions being asked of evidence carrying no date.
These pin the plumbing so it cannot silently regress: the date must reach provenance (for the reader)
and effective_at (for anything that filters on time), and an unparseable date must produce nothing
rather than something wrong.
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from benchmarks.full.locomo import (convert_sample, epoch_for, session_dates,
                                    when_for)


def test_session_dates_extracts_every_session():
    conv = {"session_1_date_time": "1:56 pm on 8 May, 2023",
            "session_2_date_time": "1:14 pm on 25 May, 2023",
            "session_1": [], "speaker_a": "Caroline"}
    assert session_dates(conv) == {
        "session_1": "1:56 pm on 8 May, 2023",
        "session_2": "1:14 pm on 25 May, 2023",
    }


def test_when_for_tolerates_derived_session_keys():
    dates = {"session_1": "1:56 pm on 8 May, 2023"}
    assert when_for("session_1", dates) == "1:56 pm on 8 May, 2023"
    assert when_for("session_1_observation", dates) == "1:56 pm on 8 May, 2023"
    assert when_for("session_9_observation", dates) is None
    assert when_for("", dates) is None


def test_epoch_for_parses_the_documented_format():
    epoch = epoch_for("1:56 pm on 8 May, 2023")
    assert epoch is not None
    assert datetime.fromtimestamp(epoch).year == 2023
    assert datetime.fromtimestamp(epoch).month == 5


def test_epoch_for_returns_none_rather_than_a_wrong_time():
    assert epoch_for("sometime last spring") is None
    assert epoch_for("") is None
    assert epoch_for(None) is None


def _sample():
    """One conversation with a dialogue turn and a dated session, in LoCoMo's shape."""
    conv = {
        "session_1_date_time": "1:56 pm on 8 May, 2023",
        "session_1": [{"speaker": "Caroline", "dia_id": "D1:3",
                       "text": "I went to the LGBTQ support group yesterday."}],
    }
    qa = [{"question": "When did Caroline go to the support group?",
           "answer": "7 May 2023", "evidence": ["D1:3"], "category": 2}]
    return {"sample_id": "conv-26", "conversation": json.dumps(conv),
            "observation": "{}", "event_summary": "{}", "session_summary": "{}",
            "qa": json.dumps(qa)}


def test_a_dialogue_turn_carries_its_session_date():
    conv_id, records, qas = convert_sample(_sample())
    turn = next(r for r in records if r.record_id.endswith("-d1-3"))
    assert turn.provenance["when"] == "1:56 pm on 8 May, 2023"
    assert turn.provenance["speaker"] == "Caroline"
    assert turn.effective_at is not None
    assert len(qas) == 1 and qas[0].evidence_ids == [f"{conv_id}-d1-3"]


def test_an_undated_conversation_still_converts():
    sample = _sample()
    sample["conversation"] = json.dumps({"session_1": [{"speaker": "A", "dia_id": "D1:1",
                                                        "text": "hello"}]})
    _conv_id, records, _qas = convert_sample(sample)
    turn = next(r for r in records if r.record_id.endswith("-d1-1"))
    assert "when" not in turn.provenance
    assert turn.effective_at is None


def test_the_reader_is_told_the_speaker_and_the_date():
    """End of the chain: the text the model sees includes what the substrate holds."""
    from aletheia.reader import _format_evidence

    class _Item:
        def __init__(self):
            self.record_id = "rec"
            self.speaker = "Caroline"
            self.when = "1:56 pm on 8 May, 2023"
            self.text = "I went to the support group yesterday."

    rendered = _format_evidence([_Item()])
    assert "Caroline" in rendered and "8 May, 2023" in rendered
    assert "rec" not in rendered, "internal record ids must not be shown to the reader"