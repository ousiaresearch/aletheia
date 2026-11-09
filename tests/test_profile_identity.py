"""Profile identity: a scope derived from who the profile is, never from a basename."""

from __future__ import annotations

from pathlib import Path

import pytest

from aletheia.profile_identity import (
    default_store_path,
    effective_read_grants,
    effective_write_grants,
    hermes_root,
    parse_scopes,
    profile_name,
    profile_scope,
)


def test_default_home_is_default_profile(tmp_path):
    root = tmp_path / ".hermes"
    root.mkdir()
    assert profile_name(root, default_root=root) == "default"
    assert profile_scope(root, default_root=root) == "profile:default"


def test_named_profile_uses_its_directory_name(tmp_path):
    root = tmp_path / ".hermes"
    home = root / "profiles" / "isildur"
    home.mkdir(parents=True)
    assert profile_name(home, default_root=root) == "isildur"
    assert profile_scope(home, default_root=root) == "profile:isildur"


def test_agent_identity_wins_over_the_path(tmp_path):
    root = tmp_path / ".hermes"
    home = root / "profiles" / "stale-name"
    home.mkdir(parents=True)
    assert profile_name(home, "palantir", default_root=root) == "palantir"
    assert profile_name(home, "  Palantir  ", default_root=root) == "palantir"


def test_two_homes_named_hermes_do_not_collide(tmp_path):
    first = tmp_path / "a" / "hermes"
    second = tmp_path / "b" / "hermes"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    one = profile_name(first, default_root=tmp_path / "a" / ".hermes")
    two = profile_name(second, default_root=tmp_path / "b" / ".hermes")
    assert one.startswith("custom-") and two.startswith("custom-")
    assert one != two


def test_no_scope_ever_contains_a_dot_directory_fragment(tmp_path):
    """The live store's old scope was ``profile-.hermes`` — a path fragment, not an identity."""
    root = tmp_path / ".hermes"
    root.mkdir()
    assert profile_scope(root, default_root=root) != "profile-.hermes"
    assert "." not in profile_scope(root, default_root=root)


def test_names_are_normalised_to_a_safe_charset(tmp_path):
    root = tmp_path / ".hermes"
    home = root / "profiles" / "Mixed Case"
    home.mkdir(parents=True)
    name = profile_name(home, default_root=root)
    # Cleaning is many-to-one, so a changed name carries a digest of the original.
    assert name.startswith("mixed-case-")


def test_distinct_names_cannot_collide(tmp_path):
    root = tmp_path / ".hermes"
    names = set()
    for raw in ("foo bar", "foo.bar", "foo-bar", "foo_bar", "x" * 90, "x" * 91):
        home = root / "profiles" / raw
        home.mkdir(parents=True, exist_ok=True)
        names.add(profile_name(home, default_root=root))
    assert len(names) == 6, names


def test_store_is_host_level_and_shared(tmp_path):
    root = tmp_path / ".hermes"
    profile_home = root / "profiles" / "isildur"
    profile_home.mkdir(parents=True)
    assert hermes_root(profile_home) == root
    assert default_store_path(profile_home) == root / "aletheia" / "aletheia.sqlite3"
    assert default_store_path(root) == root / "aletheia" / "aletheia.sqlite3"
    # Both profiles resolve to the same file: that is the point.
    assert default_store_path(profile_home) == default_store_path(root)


def test_parse_scopes_accepts_list_and_string_and_drops_unknown():
    assert parse_scopes(["profile:a", "shared"]) == ("profile:a", "shared")
    assert parse_scopes("profile:a, shared  project:x") == ("profile:a", "shared", "project:x")
    assert parse_scopes("not a scope") == ()
    assert parse_scopes(None) == ()
    assert parse_scopes(["profile:a", "profile:a"]) == ("profile:a",)
    # A bare string must never be iterated character by character.
    assert parse_scopes("shared") == ("shared",)


def test_policy_alone_grants_nothing():
    own = "profile:default"
    absent = {"present": False, "profiles": {}, "error": ""}
    # A profile's own config is a request, never an authority.
    assert effective_read_grants(own, {"read_scopes": ["profile:isildur"]}, {}, absent) == (own,)
    assert effective_read_grants(own, {"shared": True}, {}, absent) == (own,)
    assert effective_write_grants(own, {"shared_write": True}, absent) == (own,)


def test_policy_grants_the_requested_scope_and_nothing_else():
    own = "profile:default"
    policy = {"present": True, "error": "", "profiles": {"default": {"read_scopes": ["profile:isildur"]}}}
    entry = policy["profiles"]["default"]
    assert effective_read_grants(own, {"read_scopes": ["profile:isildur"]}, entry, policy) == \
        (own, "profile:isildur")
    # Asking for a scope the policy does not name is ignored.
    assert effective_read_grants(own, {"read_scopes": ["profile:palantir"]}, entry, policy) == (own,)
    # Reads never confer writes.
    assert effective_write_grants(own, entry, policy) == (own,)


def test_policy_controls_shared_writes():
    own = "profile:default"
    policy = {"present": True, "error": "", "profiles": {"default": {"shared": True, "shared_write": True}}}
    entry = policy["profiles"]["default"]
    assert effective_read_grants(own, {}, entry, policy) == (own, "shared")
    assert effective_write_grants(own, entry, policy) == (own, "shared")


def test_a_broken_policy_grants_nothing():
    own = "profile:default"
    policy = {"present": True, "error": "policy unreadable", "profiles": {"default": {"read_scopes": ["profile:isildur"]}}}
    entry = policy["profiles"]["default"]
    assert effective_read_grants(own, {"read_scopes": ["profile:isildur"]}, entry, policy) == (own,)