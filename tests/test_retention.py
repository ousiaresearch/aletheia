"""W2.6 slice 1 — retention windows as policy, and a sweep that uses the erasure path.

The properties that matter, and why each is a test rather than a sentence:

* an expired record disappears from recall *without* being deleted, and the journal still has it —
  hiding is reversible, deleting is not;
* the sweep deletes through ``Substrate.delete()``, so a derived artifact citing the record goes with
  it instead of dangling;
* a policy change does not rewrite a record: applying a window marks, and the record's own bytes are
  untouched;
* an existing mark is not silently moved by a later, shorter policy unless the caller asks;
* nothing here crosses a scope, and an ungranted caller gets zeros rather than partial work.
"""

from __future__ import annotations

import time

import pytest

from aletheia.retention import (
    Policy,
    apply_policy,
    expired_among,
    expired_record_ids,
    get_policy,
    set_policy,
    sweep,
)
from aletheia.retrieval import recall
from aletheia.scope import ScopeContext, ScopeEngine
from aletheia.substrate import Record, Substrate


@pytest.fixture
def engine():
    return ScopeEngine(known_scopes={"alpha", "beta"})


@pytest.fixture
def sub(tmp_path, engine):
    substrate = Substrate(tmp_path / "aletheia.db", scope_engine=engine)
    yield substrate
    substrate.close()


def ctx_for(*scopes: str) -> ScopeContext:
    return ScopeContext(granted_scopes=frozenset(scopes), user_authored=True)


def claim(sub, record_id, text, *, scope="alpha", source_ids=None):
    sub.put(Record(record_id=record_id, record_type="Claim", scope=scope, epistemic_status="derived",
                   payload={"text": text, "confidence": 0.9}, attribution="agent",
                   source_ids=list(source_ids or [])), ctx_for(scope))


# --------------------------------------------------------------------------- policy


def test_a_window_is_stored_and_read_back(sub):
    assert set_policy(sub, ctx_for("alpha"), "alpha", 7) is True
    policy = get_policy(sub, ctx_for("alpha"), "alpha")
    assert policy == Policy(scope="alpha", days=7.0)
    assert policy.seconds == 7 * 86400


def test_no_policy_is_not_keep_forever(sub):
    assert get_policy(sub, ctx_for("alpha"), "alpha") is None


def test_an_ungranted_scope_has_no_policy_and_cannot_set_one(sub):
    assert set_policy(sub, ctx_for("beta"), "alpha", 30) is False
    assert get_policy(sub, ctx_for("beta"), "alpha") is None
    assert sub._conn.execute("SELECT COUNT(*) FROM retention_policy").fetchone()[0] == 0


def test_a_negative_window_is_refused(sub):
    with pytest.raises(ValueError):
        set_policy(sub, ctx_for("alpha"), "alpha", -1)


# --------------------------------------------------------------------------- marking


def test_applying_a_window_marks_records_without_rewriting_them(sub):
    claim(sub, "c1", "the buoy is red")
    set_policy(sub, ctx_for("alpha"), "alpha", 1)
    before = sub._conn.execute("SELECT payload, created_at FROM records WHERE record_id='c1'").fetchone()

    report = apply_policy(sub, ctx_for("alpha"), "alpha")
    after = sub._conn.execute("SELECT payload, created_at FROM records WHERE record_id='c1'").fetchone()

    assert report["marked"] == 1
    assert before == after, "a policy change must not rewrite the record"


def test_a_shorter_policy_does_not_move_an_existing_mark_unless_asked(sub):
    claim(sub, "c1", "the buoy is red")
    ctx = ctx_for("alpha")
    set_policy(sub, ctx, "alpha", 30)
    apply_policy(sub, ctx, "alpha")
    first = sub._conn.execute("SELECT expires_at FROM retention_marks WHERE record_id='c1'").fetchone()[0]

    set_policy(sub, ctx, "alpha", 1)
    apply_policy(sub, ctx, "alpha")
    unchanged = sub._conn.execute("SELECT expires_at FROM retention_marks WHERE record_id='c1'").fetchone()[0]
    assert unchanged == first

    apply_policy(sub, ctx, "alpha", overwrite=True)
    moved = sub._conn.execute("SELECT expires_at FROM retention_marks WHERE record_id='c1'").fetchone()[0]
    assert moved < first


def test_expiry_is_measured_from_creation_not_from_the_policy(sub):
    claim(sub, "c1", "the buoy is red")
    created = sub._conn.execute("SELECT created_at FROM records WHERE record_id='c1'").fetchone()[0]
    set_policy(sub, ctx_for("alpha"), "alpha", 2)
    apply_policy(sub, ctx_for("alpha"), "alpha")
    expires = sub._conn.execute("SELECT expires_at FROM retention_marks WHERE record_id='c1'").fetchone()[0]
    assert expires == pytest.approx(created + 2 * 86400)


# --------------------------------------------------------------------------- recall


def test_an_expired_record_leaves_recall_without_being_deleted(sub):
    ctx = ctx_for("alpha")
    claim(sub, "c1", "the lighthouse needs paint")
    set_policy(sub, ctx, "alpha", 0.00001)
    apply_policy(sub, ctx, "alpha")
    time.sleep(1.2)  # a hair past the window; the policy is deliberately tiny

    items, receipt = recall(sub, "lighthouse", ctx, mode="lexical", limit=5)
    assert [item.record_id for item in items] == []
    assert any("retention" in reason for reason in receipt.exclusions)
    assert sub.get("c1", ctx) is not None, "hiding is not deleting"


def test_a_live_record_is_unaffected_by_a_policy(sub):
    ctx = ctx_for("alpha")
    claim(sub, "c1", "the lighthouse needs paint")
    set_policy(sub, ctx, "alpha", 30)
    apply_policy(sub, ctx, "alpha")

    items, _ = recall(sub, "lighthouse", ctx, mode="lexical", limit=5)
    assert [item.record_id for item in items] == ["c1"]


def test_expired_among_only_answers_about_the_ids_it_was_given(sub):
    ctx = ctx_for("alpha")
    claim(sub, "c1", "one")
    claim(sub, "c2", "two")
    set_policy(sub, ctx, "alpha", 0.00001)
    apply_policy(sub, ctx, "alpha")
    time.sleep(1.2)

    assert expired_among(sub, ["c1"]) == {"c1"}
    assert expired_among(sub, []) == set()


# --------------------------------------------------------------------------- sweep


def test_a_dry_run_sweep_reports_and_deletes_nothing(sub):
    ctx = ctx_for("alpha")
    claim(sub, "c1", "one")
    set_policy(sub, ctx, "alpha", 0.00001)
    apply_policy(sub, ctx, "alpha")
    time.sleep(1.2)

    report = sweep(sub, ctx, "alpha")
    assert report == {"expired": 1, "deleted": 0, "skipped": 0, "dry_run": True}
    assert sub.get("c1", ctx) is not None


def test_a_sweep_deletes_through_the_erasure_path(sub):
    ctx = ctx_for("alpha")
    claim(sub, "c1", "one")
    claim(sub, "c2", "two")
    # A derived artifact citing c1 must go with it: delete() cascades, a raw DELETE would not.
    sub.put(Record(record_id="summary", record_type="Summary", scope="alpha",
                   epistemic_status="derived", payload={"text": "one, summarised"},
                   attribution="system", source_ids=["c1"]), ctx)

    set_policy(sub, ctx, "alpha", 0.00001)
    apply_policy(sub, ctx, "alpha")
    time.sleep(1.2)
    # Only the two claims are inside the window's short expiry; keep the summary out of the sweep.
    sub._conn.execute("DELETE FROM retention_marks WHERE record_id = 'summary'")
    sub._conn.commit()

    report = sweep(sub, ctx, "alpha", dry_run=False)
    assert report["deleted"] >= 1
    assert report["skipped"] == 0
    assert sub.get("c1", ctx) is None
    assert sub.get("summary", ctx) is None, "a derived artifact does not outlive its source"
    assert sub._conn.execute("SELECT COUNT(*) FROM retention_marks WHERE record_id='c1'").fetchone()[0] == 0


def test_an_ungranted_sweep_does_nothing(sub):
    claim(sub, "c1", "one", scope="beta")
    assert sweep(sub, ctx_for("alpha"), "beta", dry_run=False) == {
        "expired": 0, "deleted": 0, "skipped": 0, "dry_run": False}


def test_the_cli_sets_applies_and_refuses_to_sweep_without_confirm(tmp_path, capsys):
    import json

    from aletheia import cli

    db = tmp_path / "cli.db"
    engine = ScopeEngine(known_scopes={"alpha"})
    sub = Substrate(db, engine)
    try:
        claim(sub, "c1", "the lighthouse needs paint")
    finally:
        sub.close()

    base = ["--db", str(db), "retention", "--scope", "alpha"]
    assert cli.main(base + ["--set-days", "30", "--apply"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["policy"] == 30.0 and report["marked"] == 1

    # Without --confirm a sweep is a report, whatever the window says.
    assert cli.main(base + ["--sweep"]) == 0
    sweep_report = json.loads(capsys.readouterr().out)
    assert sweep_report["dry_run"] is True and sweep_report["deleted"] == 0


def test_the_cli_refuses_to_apply_without_a_window(tmp_path, capsys):
    import json

    from aletheia import cli

    db = tmp_path / "cli.db"
    engine = ScopeEngine(known_scopes={"alpha"})
    Substrate(db, engine).close()

    assert cli.main(["--db", str(db), "retention", "--scope", "alpha", "--apply"]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "refused"


def test_expired_ids_are_scoped(sub):
    ctx = ctx_for("alpha")
    claim(sub, "c1", "one")
    set_policy(sub, ctx, "alpha", 0.00001)
    apply_policy(sub, ctx, "alpha")
    time.sleep(1.2)

    assert expired_record_ids(sub, "alpha") == ["c1"]
    assert expired_record_ids(sub, "beta") == []