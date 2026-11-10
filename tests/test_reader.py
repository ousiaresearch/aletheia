"""Tests for the reading stage.

These are the guarantees that make a reader safe to promote from measurement into the product, so they
are asserted rather than documented: the reader cannot see the substrate, cannot cite evidence it was
not given, and cannot answer when no reader is configured.
"""

from __future__ import annotations

import json

import pytest

from aletheia import llm_client, reader


class _Item:
    def __init__(self, record_id: str, text: str):
        self.record_id = record_id
        self.text = text


ITEMS = [_Item("rec-1", "Caroline went to the support group on 7 May 2023."),
         _Item("rec-2", "Melanie painted a sunrise in 2022.")]


@pytest.fixture()
def fake_llm(monkeypatch):
    """Install a fake transport and return a setter for the model's reply."""
    state = {"reply": "{}", "prompt": None}

    class _Config:
        provider = "test"
        model = "fake-model"

    monkeypatch.setattr(llm_client, "get_config", lambda api_key=None: _Config())

    def _complete(prompt, **kwargs):
        state["prompt"] = prompt
        return state["reply"]

    monkeypatch.setattr(llm_client, "complete_text", _complete)

    def set_reply(obj):
        state["reply"] = obj if isinstance(obj, str) else json.dumps(obj)

    return set_reply, state


def test_no_configured_reader_returns_none(monkeypatch):
    """None means the stage did not run. Returning a zero or an empty answer would report the absence
    of a measurement as a measurement of absence."""
    monkeypatch.setattr(llm_client, "get_config", lambda api_key=None: None)
    assert reader.read("anything", ITEMS) is None
    assert reader.configured() is False


def test_answer_maps_item_numbers_to_record_ids(fake_llm):
    set_reply, _state = fake_llm
    set_reply({"reasoning": "[1] gives the date.", "answer": "7 May 2023", "abstain": False,
               "cited": [1]})
    answer = reader.read("When did Caroline go to the support group?", ITEMS)
    assert answer is not None
    assert answer.text == "7 May 2023"
    assert answer.cited == ["rec-1"]
    assert answer.abstained is False
    assert answer.model == "test:fake-model"
    assert answer.prompt_version == reader.PROMPT_VERSION


def test_citations_it_was_not_given_are_dropped_and_counted(fake_llm):
    """A model that invents evidence must not be able to make it look retrieved."""
    set_reply, _state = fake_llm
    set_reply({"answer": "7 May 2023", "abstain": False, "cited": [1, 7, 99]})
    answer = reader.read("When?", ITEMS)
    assert answer is not None
    assert answer.cited == ["rec-1"]
    assert answer.dropped_citations == 2


def test_abstention_yields_no_text(fake_llm):
    set_reply, _state = fake_llm
    set_reply({"answer": "", "abstain": True, "cited": [], "reasoning": "not in evidence"})
    answer = reader.read("What is Caroline's salary?", ITEMS)
    assert answer is not None
    assert answer.abstained is True
    assert answer.text == ""


def test_empty_answer_field_is_treated_as_abstention(fake_llm):
    set_reply, _state = fake_llm
    set_reply({"reasoning": "nothing", "answer": "   ", "abstain": False, "cited": []})
    answer = reader.read("What is Caroline's salary?", ITEMS)
    assert answer is not None
    assert answer.abstained is True


def test_unparseable_response_is_not_an_answer(fake_llm):
    set_reply, _state = fake_llm
    set_reply("I think it was probably sometime in May.")
    answer = reader.read("When?", ITEMS)
    assert answer is not None
    assert answer.abstained is True
    assert answer.text == ""


def test_fenced_json_is_accepted(fake_llm):
    """Local models fence their JSON constantly; refusing it would measure the fence, not the reader."""
    set_reply, _state = fake_llm
    set_reply('```json\n{"answer": "2022", "abstain": false, "cited": [2]}\n```')
    answer = reader.read("When did Melanie paint a sunrise?", ITEMS)
    assert answer is not None
    assert answer.text == "2022"
    assert answer.cited == ["rec-2"]


def test_no_retrieved_items_means_abstention_without_calling_a_model(fake_llm, monkeypatch):
    called = {"n": 0}

    def _complete(prompt, **kwargs):
        called["n"] += 1
        return "{}"

    monkeypatch.setattr(llm_client, "complete_text", _complete)
    answer = reader.read("When?", [])
    assert answer is not None and answer.abstained is True
    assert called["n"] == 0, "the reader called a model with no evidence to read"


def test_prompt_numbers_every_item_and_asks_for_reasoning(fake_llm):
    set_reply, state = fake_llm
    set_reply({"answer": "7 May 2023", "abstain": False, "cited": [1]})
    reader.read("When did Caroline go to the support group?", ITEMS)
    prompt = state["prompt"]
    assert "[1]" in prompt and "[2]" in prompt
    assert "rec-1" not in prompt, "record ids must not be exposed to the reader"
    assert "reasoning" in prompt
    assert "ONLY the numbered evidence" in prompt


def test_reader_module_cannot_reach_the_substrate():
    """Structural, not conventional: the reading stage has no write path, so its output can never
    become a record and design invariant 6 (composite artifacts are never injectable) holds."""
    import inspect

    source = inspect.getsource(reader)
    for forbidden in ("from aletheia.substrate", "import substrate", "substrate.put",
                      "record_receipt", "vector_put"):
        assert forbidden not in source, f"reader.py reaches the substrate: {forbidden}"