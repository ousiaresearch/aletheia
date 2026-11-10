"""Tests for entity extraction precision.

The graph is only as useful as its entities. Before this filter, any sentence-initial capital counted,
so `wow`, `thanks`, `that` and `they` were entities, and a co-mention diagnostic bridged on them: what
looked like 74% of missed evidence being reachable through a shared entity was 6.9% once the bridges had
to be selective. Precision here is what stops the graph from lying about how connected the corpus is.
"""

from __future__ import annotations

from aletheia.entity_link import extract_entities


def test_discourse_markers_are_not_entities():
    entities = extract_entities("Wow, thanks! That was great. They said it was really nice.")
    assert entities == []


def test_names_survive_the_filter():
    entities = extract_entities("Caroline went to the LGBTQ support group with Melanie.")
    assert "caroline" in entities
    assert "melanie" in entities
    assert "lgbtq" in entities


def test_capitalised_topics_survive():
    """A capitalised common noun at the head of a sentence is usually a topic, not noise."""
    entities = extract_entities("Adoption agencies matter. Psychology is a field she considered.")
    assert "adoption" in entities
    assert "psychology" in entities


def test_sentence_initial_verbs_and_pronouns_are_dropped():
    entities = extract_entities("I think it was good. She said we should go. He told me.")
    assert entities == []


def test_multi_word_capitalised_sequences_are_captured():
    entities = extract_entities("She met Someone From Boston at the conference.")
    assert any(" " in entity for entity in entities)


def test_dates_are_not_swallowed_whole():
    """A year is not an entity: 2022 has no letter, so the capitalised-token rule does not apply."""
    entities = extract_entities("Melanie painted it in 2022.")
    assert "melanie" in entities
    assert all("2022" not in entity for entity in entities)


def test_output_is_deduplicated_and_ordered():
    entities = extract_entities("Caroline and Caroline and Melanie.")
    assert entities == ["caroline", "melanie"]