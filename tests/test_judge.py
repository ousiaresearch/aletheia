"""Tests for the answer judge.

A judged number is only publishable with its judge declared and repeatable, so the properties that make
it usable are asserted: it fails closed with no provider, it never sees the retrieved evidence (only
question, gold and prediction), it refuses a response it cannot parse rather than guessing a verdict,
and it records the model and prompt version with every judgement.
"""

from __future__ import annotations

import json

import pytest

from aletheia import llm_client
from benchmarks import judge


@pytest.fixture()
def fake_judge(monkeypatch):
    state = {"reply": "{}", "prompt": None}

    class _Config:
        provider = "test"
        model = "judge-model"

    monkeypatch.setattr(llm_client, "get_config", lambda api_key=None: _Config())

    def _complete(prompt, **kwargs):
        state["prompt"] = prompt
        return state["reply"]

    monkeypatch.setattr(llm_client, "complete_text", _complete)

    def set_reply(value):
        state["reply"] = value if isinstance(value, str) else json.dumps(value)

    return set_reply, state


def test_no_provider_means_no_judgement(monkeypatch):
    monkeypatch.setattr(llm_client, "get_config", lambda api_key=None: None)
    assert judge.judge_one("q", "gold", "predicted") is None


def test_correct_verdict_is_parsed_with_its_provenance(fake_judge):
    set_reply, _state = fake_judge
    set_reply({"verdict": "CORRECT", "reason": "same fact, different words"})
    result = judge.judge_one("When?", "7 May 2023", "7 May 2023")
    assert result["correct"] is True
    assert result["judge"] == "test:judge-model"
    assert result["prompt_version"] == judge.JUDGE_PROMPT_VERSION


def test_incorrect_verdict_is_parsed(fake_judge):
    set_reply, _state = fake_judge
    set_reply({"verdict": "incorrect", "reason": "wrong year"})
    assert judge.judge_one("When?", "7 May 2023", "8 May 2023")["correct"] is False


def test_unparseable_reply_is_not_a_verdict(fake_judge):
    set_reply, _state = fake_judge
    set_reply("I would say that looks correct to me.")
    assert judge.judge_one("q", "gold", "predicted") is None


def test_an_unknown_verdict_word_is_refused(fake_judge):
    set_reply, _state = fake_judge
    set_reply({"verdict": "PROBABLY", "reason": "unsure"})
    assert judge.judge_one("q", "gold", "predicted") is None


def test_fenced_json_is_accepted(fake_judge):
    set_reply, _state = fake_judge
    set_reply('```json\n{"verdict": "CORRECT", "reason": "matches"}\n```')
    assert judge.judge_one("q", "gold", "gold")["correct"] is True


def test_the_judge_never_sees_the_retrieved_evidence(fake_judge):
    """A judge shown the evidence can reward an answer the evidence supports over one that matches the
    gold answer, which is a different question than the benchmark asks."""
    set_reply, state = fake_judge
    set_reply({"verdict": "CORRECT", "reason": "ok"})
    judge.judge_one("When did Caroline go?", "7 May 2023", "7 May 2023")
    prompt = state["prompt"]
    assert "7 May 2023" in prompt
    assert "Evidence" not in prompt and "evidence below" not in prompt


def test_an_empty_prediction_is_presented_as_empty_not_blank(fake_judge):
    set_reply, state = fake_judge
    set_reply({"verdict": "INCORRECT", "reason": "no answer"})
    judge.judge_one("q", "gold", "")
    assert "(empty)" in state["prompt"]