"""Retention: how long a scope keeps a record, and the sweep that ends it.

Two decisions worth stating, because both are visible in the schema:

* **The window is policy, not a record column.** Putting ``expires_at`` on the record would make a
  policy change a rewrite of history — every record edited because an operator shortened a window.
  The record is the thing this substrate promises not to rewrite, so the window lives in side tables
  (``retention_policy`` per scope, ``retention_marks`` per record) and the record stays untouched.
* **Deletion goes through ``Substrate.delete()``.** Not a ``DELETE FROM records``: erasure has to
  cascade through lineage, scrub the index and the WAL, and journal itself, and there is exactly one
  path that does all of that. A sweep that talks to the table directly would leave derived artifacts
  pointing at a record that no longer exists.

Expired records stop appearing in recall *before* they are purged: hiding is reversible and free,
deleting is neither. A sweep is an explicit, reportable act, and ``dry_run`` is the default in the
CLI for the same reason consolidation reports before it marks.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from aletheia.scope import ScopeContext
from aletheia.substrate import Substrate

SECONDS_PER_DAY = 86400.0


@dataclass(frozen=True)
class Policy:
    scope: str
    days: float

    @property
    def seconds(self) -> float:
        return self.days * SECONDS_PER_DAY


def set_policy(sub: Substrate, ctx: ScopeContext, scope: str, days: float) -> bool:
    """Set (or replace) one scope's window. Refused for a scope the caller cannot write."""
    if days < 0:
        raise ValueError("a retention window is days >= 0")
    if ctx is None or scope not in ctx.granted_scopes:
        return False
    if not sub.scope_engine.authorize_read(scope, ctx, "").allowed:
        return False
    with sub._lock:
        own = None
        try:
            own = sub._begin_write("aletheia_retention_policy")
            sub._conn.execute(
                "INSERT INTO retention_policy (scope, days, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(scope) DO UPDATE SET days=excluded.days, updated_at=excluded.updated_at",
                (scope, float(days), time.time()),
            )
            sub._finish_write("aletheia_retention_policy", own)
            return True
        except Exception:
            if own is not None:
                sub._abort_write("aletheia_retention_policy", own)
            return False


def get_policy(sub: Substrate, ctx: ScopeContext, scope: str) -> Policy | None:
    """The scope's window, or None when no policy has been set — absence is not "keep forever"."""
    if ctx is None or scope not in ctx.granted_scopes:
        return None
    if not sub.scope_engine.authorize_read(scope, ctx, "").allowed:
        return None
    row = sub._conn.execute("SELECT days FROM retention_policy WHERE scope = ?", (scope,)).fetchone()
    return None if row is None else Policy(scope=scope, days=float(row[0]))


def apply_policy(sub: Substrate, ctx: ScopeContext, scope: str, *, at: float | None = None,
                 overwrite: bool = False) -> dict:
    """Mark records in ``scope`` as expiring ``days`` after they were created.

    ``overwrite=False`` leaves marks that already exist alone, so a shorter window applied today
    cannot silently pull an older record's expiry forward on a schedule somebody already published.
    """
    policy = get_policy(sub, ctx, scope)
    if policy is None:
        return {"marked": 0, "policy": None}
    now = time.time() if at is None else float(at)
    rows = sub._conn.execute("SELECT record_id, created_at FROM records WHERE scope = ?",
                             (scope,)).fetchall()
    existing = {row[0] for row in sub._conn.execute(
        "SELECT record_id FROM retention_marks WHERE scope = ?", (scope,)).fetchall()}
    marked = 0
    with sub._lock:
        own = None
        try:
            own = sub._begin_write("aletheia_retention_apply")
            for record_id, created_at in rows:
                if record_id in existing and not overwrite:
                    continue
                expires_at = float(created_at or now) + policy.seconds
                sub._conn.execute(
                    "INSERT INTO retention_marks (scope, record_id, expires_at, reason) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT(scope, record_id) DO UPDATE SET "
                    "expires_at=excluded.expires_at, reason=excluded.reason",
                    (scope, record_id, expires_at, f"policy:{policy.days:g}d"),
                )
                marked += 1
            sub._finish_write("aletheia_retention_apply", own)
        except Exception:
            if own is not None:
                sub._abort_write("aletheia_retention_apply", own)
            return {"marked": 0, "policy": policy.days}
    return {"marked": marked, "policy": policy.days}


def expired_record_ids(sub: Substrate, scope: str, *, at: float | None = None,
                       limit: int = 1000) -> list[str]:
    """Records whose window has closed. Scope-level only: callers authorise before asking."""
    now = time.time() if at is None else float(at)
    return [row[0] for row in sub._conn.execute(
        "SELECT record_id FROM retention_marks WHERE scope = ? AND expires_at <= ? "
        "ORDER BY expires_at, record_id LIMIT ?", (scope, now, limit)).fetchall()]


def expired_among(sub: Substrate, record_ids: list[str], *, at: float | None = None) -> set[str]:
    """The subset of ``record_ids`` whose window has closed — the filter recall needs."""
    if not record_ids:
        return set()
    now = time.time() if at is None else float(at)
    placeholders = ", ".join("?" for _ in record_ids)
    rows = sub._conn.execute(
        f"SELECT record_id FROM retention_marks WHERE expires_at <= ? AND record_id IN ({placeholders})",
        (now, *record_ids)).fetchall()
    return {row[0] for row in rows}


def sweep(sub: Substrate, ctx: ScopeContext, scope: str, *, at: float | None = None,
          dry_run: bool = True, limit: int = 1000) -> dict:
    """Purge expired records through ``Substrate.delete()`` — the one erasure path there is.

    Reports before it acts unless ``dry_run=False``. A record that cannot be deleted by this caller
    is counted as skipped rather than silently left for the next run to pretend it removed.
    """
    if ctx is None or scope not in ctx.granted_scopes:
        return {"expired": 0, "deleted": 0, "skipped": 0, "dry_run": dry_run}
    if not sub.scope_engine.authorize_read(scope, ctx, "").allowed:
        return {"expired": 0, "deleted": 0, "skipped": 0, "dry_run": dry_run}

    targets = expired_record_ids(sub, scope, at=at, limit=limit)
    deleted = skipped = 0
    if not dry_run:
        for record_id in targets:
            try:
                if sub.delete(record_id, ctx, reason="retention window closed"):
                    deleted += 1
                    sub._conn.execute("DELETE FROM retention_marks WHERE scope = ? AND record_id = ?",
                                      (scope, record_id))
                else:
                    skipped += 1
            except Exception:
                skipped += 1
        with sub._lock:
            own = None
            try:
                own = sub._begin_write("aletheia_retention_sweep")
                sub._finish_write("aletheia_retention_sweep", own)
            except Exception:
                if own is not None:
                    sub._abort_write("aletheia_retention_sweep", own)
    return {"expired": len(targets), "deleted": deleted, "skipped": skipped, "dry_run": dry_run}