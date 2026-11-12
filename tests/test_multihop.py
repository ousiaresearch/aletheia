"""Tests for the second-hop query the multi-hop mode generates.

The second hop is the one retrieval mechanism with a defensible mechanism behind it: three cheaper ones
were measured and failed, and all three shared an inability to reason about what the missing evidence
would *say*. What these tests pin is that the hop is cheap when it does not fire, and that a model reply
cannot smuggle prose or an unbounded string into a search query.
"""

from __future__ import annotations

import json

import pytest

from aletheia import llm_client
from benchmarks import run as bench


class _Item:
    def __init__(self, record_id: str, text: str):
        self.record_id = record_id
        self.text = text


ITEMS = [_Item("r1", "Caroline went to the support group on 7 May 2023."),
         _Item("r2", "Melanie painted a sunrise.")]


@pytest.fixture()
def fake_model(monkeypatch):
    state = {"reply": "", "prompt": None}

    class _Config:
        provider = "test"
        model = "hop-model"

    monkeypatch.setattr(llm_client, "get_config", lambda api_key=None: _Config())

    def _complete(prompt, **kwargs):
        state["prompt"] = prompt
        return state["reply"]

    monkeypatch.setattr(llm_client, "complete_text", _complete)
    return state


def test_no_provider_means_no_hop(monkeypatch):
    monkeypatch.setattr(llm_client, "get_config", lambda api_key=None: None)
    assert bench._second_hop_query("q", ITEMS) is None


def test_none_reply_means_the_evidence_was_sufficient(fake_model):
    fake_model["reply"] = "NONE"
    assert bench._second_hop_query("q", ITEMS) is None
    fake_model["reply"] = "None."
    assert bench._second_hop_query("q", ITEMS) is None


def test_a_short_query_is_returned(fake_model):
    fake_model["reply"] = "Melanie sunrise painting year\n"
    assert bench._second_hop_query("q", ITEMS) == "Melanie sunrise painting year"


def test_an_over_long_reply_is_refused(fake_model):
    """A model that ignores the instruction and writes a paragraph must not become a search query."""
    fake_model["reply"] = "I think you should look for the passage where Caroline discusses " \
                          "her family history in detail, including the part about her mother " \
                          "and the adoption agencies she contacted over several months."
    assert bench._second_hop_query("q", ITEMS) is None


def test_empty_reply_means_no_hop(fake_model):
    fake_model["reply"] = "   "
    assert bench._second_hop_query("q", ITEMS) is None


def test_no_items_means_no_hop(fake_model):
    assert bench._second_hop_query("q", []) is None
    assert fake_model["prompt"] is None, "the model was called with no evidence"


def test_the_prompt_carries_the_question_and_bounded_evidence(fake_model):
    fake_model["reply"] = "NONE"
    bench._second_hop_query("When did Caroline go?", ITEMS)
    prompt = fake_model["prompt"]
    assert "When did Caroline go?" in prompt
    assert "[1]" in prompt and "Caroline went to the support group" in prompt
    assert "NONE" in prompt


def test_hop_prompt_sees_a_bounded_number_of_items(fake_model):
    fake_model["reply"] = "NONE"
    many = [_Item(f"r{i}", f"record {i}") for i in range(20)]
    bench._second_hop_query("q", many)
    assert bench.limit_for_hop(many) == 6
    assert "[7]" not in fake_model["prompt"]


def test_a_hop_failure_is_recoverable(monkeypatch):
    """A hop that dies must degrade to first-hop retrieval, and stay visible when it does."""
    class _Config:
        provider = "test"
        model = "hop-model"

    monkeypatch.setattr(llm_client, "get_config", lambda api_key=None: _Config())

    def _explode(prompt, **kwargs):
        raise TimeoutError("The read operation timed out")

    monkeypatch.setattr(llm_client, "complete_text", _explode)
    with pytest.raises(TimeoutError):
        bench._second_hop_query("q", ITEMS)


def test_label_prefixes_are_stripped_from_the_hop_query():
    assert bench.prefix_hop_name("Query: Melanie sunrise") == "Melanie sunrise"
    assert bench.prefix_hop_name("Search query: adoption agencies") == "adoption agencies"
    assert bench.prefix_hop_name("plain query") == "plain query"