"""Profile identity: a scope derived from who the profile is, never from a basename."""

from __future__ import annotations

from pathlib import Path

import pytest

from aletheia.profile_identity import (
    default_store_path,
    hermes_root,
    parse_scopes,
    profile_name,
    profile_scope,
    read_grants,
    write_grants,
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
    assert profile_name(home, default_root=root) == "mixed-case"


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


def test_grants_follow_the_opt_in_flags():
    own = "profile:default"
    assert read_grants(own, {}) == (own,)
    assert read_grants(own, {"read_scopes": ["profile:isildur"]}) == (own, "profile:isildur")
    assert read_grants(own, {"shared": True}) == (own, "shared")
    assert read_grants(own, {"shared": "false"}) == (own,)
    assert write_grants(own, {"shared": True}) == (own,)
    assert write_grants(own, {"shared": True, "shared_write": True}) == (own, "shared")
    assert write_grants(own, {"read_scopes": ["profile:isildur"]}) == (own,)  # reads never grant writes