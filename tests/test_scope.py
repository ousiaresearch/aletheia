"""Scope engine tests.

The first class is the Phase 0 regression target: a query about memory providers
must return ZERO ACS release history. Expectations follow the review fixes for
a37d175 (F1-F6, F3 in particular).
"""

import pytest

from aletheia.scope import ScopeContext, ScopeEngine, references_project
from aletheia.substrate import Record

KNOWN = {"ariel-continuity", "acs-project", "memory-providers", "identity"}
MARKERS = {
    "acs-project": {"ACS", "Listening Commonwealth", "Constitutional Convention Letters"},
}


@pytest.fixture
def engine():
    return ScopeEngine(known_scopes=KNOWN, markers=MARKERS)


def mem_ctx():
    return ScopeContext(granted_scopes=frozenset({"memory-providers"}))


class TestRegressionTarget:
    def test_memory_provider_query_returns_no_acs_history(self, engine):
        """REGRESSION TARGET (Phase 0): ACS material is not returned to an unrelated query."""
        acs_record = Record(
            record_id="acs1", record_type="Observation", attribution="document", source_ids=["src-1"], scope="acs-project",
            payload={"text": "RC-05 released; ACS attestation chain updated."},
        )
        readable = engine.filter_readable([acs_record], mem_ctx())
        assert readable == []

    def test_acs_returned_only_when_explicitly_granted(self, engine):
        ctx = ScopeContext(granted_scopes=frozenset({"acs-project"}))
        assert engine.authorize_read("acs-project", ctx).allowed

    def test_identity_resident_acs_fact_is_scoped_by_content(self, engine):
        """A peer-card fact naming ACS, stored under identity, is not readable under identity alone."""
        card_fact = "Owner of the ACS Constitutional Convention Letters"
        ctx = ScopeContext(granted_scopes=frozenset({"identity"}))
        decision = engine.authorize_read("identity", ctx, text=card_fact)
        assert not decision.allowed
        assert "different scope" in decision.reason

    def test_no_global_allowlist_by_default(self, engine):
        assert engine.global_allowlist == set()
        assert not engine.authorize_read("acs-project", ScopeContext()).allowed


class TestDenyByDefault:
    def test_unknown_scope_denied(self, engine):
        d = engine.authorize_read("never-heard-of-this", ScopeContext(granted_scopes=frozenset()))
        assert not d.allowed
        assert "unknown scope" in d.reason

    def test_empty_context_denies_everything(self, engine):
        for scope in engine.known_scopes:
            assert not engine.authorize_read(scope, ScopeContext()).allowed

    def test_no_context_denies(self, engine):
        assert not engine.authorize_read("memory-providers", None).allowed

    def test_engine_decisions_carry_reasons(self, engine):
        """Decisions keep their reasons for the engine's own use. Query results do not."""
        d = engine.authorize_read("acs-project", mem_ctx())
        assert not d.allowed and d.reason


class TestCrossScopeAuthorization:
    def test_cross_scope_requires_user_attribution(self, engine):
        """The agent cannot mint its own cross-scope authorization."""
        ctx = ScopeContext(granted_scopes=frozenset({"memory-providers"}), user_authorized_cross_scope=False)
        assert not engine.authorize_read("acs-project", ctx).allowed

    def test_user_authorized_cross_scope_permitted(self, engine):
        ctx = ScopeContext(
            granted_scopes=frozenset({"memory-providers"}),
            user_authorized_cross_scope=True, user_authored=True,
            cross_scope_targets=frozenset({"acs-project"}),
            authorization_source="user-typed-directive",
        )
        d = engine.authorize_read("acs-project", ctx)
        assert d.allowed
        assert "user-authorized" in d.reason

    def test_cross_scope_target_mismatch_denied(self, engine):
        ctx = ScopeContext(
            granted_scopes=frozenset({"memory-providers"}),
            user_authorized_cross_scope=True, user_authored=True,
            cross_scope_targets=frozenset({"identity"}),
            authorization_source="user-typed-directive",
        )
        assert not engine.authorize_read("acs-project", ctx).allowed

    def test_ambiguous_attribution_fails_closed(self, engine):
        """Review fix F4: no targets, or no attribution, means denial, not a blanket grant."""
        no_targets = ScopeContext(
            granted_scopes=frozenset({"memory-providers"}),
            user_authorized_cross_scope=True, user_authored=True,
            cross_scope_targets=frozenset(),
            authorization_source="user-typed-directive",
        )
        assert not engine.authorize_read("acs-project", no_targets).allowed

        no_source = ScopeContext(
            granted_scopes=frozenset({"memory-providers"}),
            user_authorized_cross_scope=True, user_authored=True,
            cross_scope_targets=frozenset({"acs-project"}),
            authorization_source=None,
        )
        assert not engine.authorize_read("acs-project", no_source).allowed


class TestWriteAuthorization:
    def test_write_needs_context(self, engine):
        rec = Record(record_id="x", record_type="Observation", attribution="document", source_ids=["src-1"], scope="memory-providers", payload={})
        assert not engine.authorize_write(rec, None).allowed

    def test_write_needs_granted_scope(self, engine):
        """Review fix F1: knowing a scope exists is not authority to write to it."""
        rec = Record(record_id="x", record_type="Observation", attribution="document", source_ids=["src-1"], scope="acs-project",
                     payload={"text": "RC-05 notes"})
        assert not engine.authorize_write(rec, mem_ctx()).allowed

    def test_unknown_scope_denied(self, engine):
        rec = Record(record_id="x", record_type="Observation", attribution="document", source_ids=["src-1"], scope="totally-unknown", payload={})
        assert not engine.authorize_write(rec, ScopeContext(granted_scopes=frozenset({"totally-unknown"}))).allowed

    def test_acs_content_refused_under_other_scope(self, engine):
        """Review fix F3: the content guard decides; it is not a no-op."""
        rec = Record(record_id="x", record_type="Observation", attribution="document", source_ids=["src-1"], scope="memory-providers",
                     payload={"text": "The Listening Commonwealth released RC-05."})
        assert not engine.authorize_write(rec, mem_ctx()).allowed

    def test_acs_content_accepted_under_its_own_scope(self, engine):
        rec = Record(record_id="x", record_type="Observation", attribution="document", source_ids=["src-1"], scope="acs-project",
                     payload={"text": "The Listening Commonwealth released RC-05."})
        ctx = ScopeContext(granted_scopes=frozenset({"acs-project"}))
        assert engine.authorize_write(rec, ctx).allowed

    def test_ordinary_content_writable(self, engine):
        rec = Record(record_id="x", record_type="Observation", attribution="document", source_ids=["src-1"], scope="memory-providers",
                     payload={"text": "Honcho uses hybrid recall."})
        assert engine.authorize_write(rec, mem_ctx()).allowed

    def test_record_without_scope_denied(self, engine):
        rec = Record(record_id="x", record_type="Observation", attribution="document", source_ids=["src-1"], scope="", payload={})
        assert not engine.authorize_write(rec, mem_ctx()).allowed

    def test_delete_needs_granted_scope_only(self, engine):
        cross = ScopeContext(
            granted_scopes=frozenset(),
            user_authorized_cross_scope=True, user_authored=True,
            cross_scope_targets=frozenset({"acs-project"}),
            authorization_source="user-typed-directive",
        )
        assert not engine.authorize_delete("acs-project", cross).allowed
        assert not engine.authorize_delete("acs-project", None).allowed
        assert engine.authorize_delete("acs-project", ScopeContext(granted_scopes=frozenset({"acs-project"}))).allowed


class TestContentScoping:
    @pytest.mark.parametrize(
        "text",
        [
            "Owner of the ACS Constitutional Convention Letters",
            "The Agentic Commonwealth Society published RC-05",
            "Founding Steward of The Listening Commonwealth",
        ],
    )
    def test_detects_project_references(self, text):
        assert references_project(text), f"should detect project reference: {text!r}"

    @pytest.mark.parametrize("text", ["prefers concise responses", "the weather is mild today", ""])
    def test_does_not_flag_ordinary_text(self, text):
        assert not references_project(text), f"false positive on: {text!r}"

    def test_marker_match_is_whole_token(self, engine):
        """'ACSX' must not match the 'ACS' marker."""
        assert engine.content_conflict("memory-providers", "ACSX is a different thing") is None
        assert engine.content_conflict("memory-providers", "The ACS charter") is not None


class TestContextTypeSafety:
    """Review fixes F5 and F6."""

    def test_string_granted_scopes_rejected(self):
        with pytest.raises(TypeError):
            ScopeContext(granted_scopes="acs-project")

    def test_string_targets_rejected(self):
        with pytest.raises(TypeError):
            ScopeContext(cross_scope_targets="acs-project")

    def test_context_is_frozen(self):
        ctx = ScopeContext(granted_scopes=frozenset({"memory-providers"}))
        with pytest.raises(Exception):
            ctx.granted_scopes = frozenset({"acs-project"})  # type: ignore[misc]
