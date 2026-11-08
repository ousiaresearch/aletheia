"""The 2.0.0 rename must not cost anybody their memory.

These tests cover the one-time move from the pre-rename layout (``<profile>/weft/weft.sqlite3``,
``<profile>/weft/provider-config.json``, ``~/.weft/weft.sqlite3``) to the Aletheia paths, including
the cases that must *not* move anything: a store already at the new path, and a missing legacy file.
"""

from __future__ import annotations

from aletheia.paths import (
    migrate_legacy_cli_store,
    migrate_legacy_config,
    migrate_legacy_store,
)


def _legacy_store(home):
    legacy = home / "weft" / "weft.sqlite3"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_bytes(b"records")
    (legacy.parent / "weft.sqlite3-wal").write_bytes(b"wal")
    return legacy


def test_legacy_store_moves_with_its_wal_and_only_once(tmp_path):
    home = tmp_path / "profile"
    legacy = _legacy_store(home)
    new = home / "aletheia" / "aletheia.sqlite3"

    assert migrate_legacy_store(new) is True
    assert new.read_bytes() == b"records"
    assert (home / "aletheia" / "aletheia.sqlite3-wal").read_bytes() == b"wal"
    assert not legacy.exists(), "the legacy store must not be left behind to be opened later"
    assert migrate_legacy_store(new) is False, "a second run must be a no-op"


def test_an_existing_new_store_wins_over_the_legacy_one(tmp_path):
    home = tmp_path / "profile"
    legacy = _legacy_store(home)
    new = home / "aletheia" / "aletheia.sqlite3"
    new.parent.mkdir(parents=True)
    new.write_bytes(b"current")

    assert migrate_legacy_store(new) is False
    assert new.read_bytes() == b"current"
    assert legacy.is_file(), "a store that lost the race stays where it is, it is not deleted"


def test_no_legacy_store_is_not_an_error(tmp_path):
    assert migrate_legacy_store(tmp_path / "profile" / "aletheia" / "aletheia.sqlite3") is False


def test_legacy_provider_config_moves_beside_the_new_store(tmp_path):
    home = tmp_path / "profile"
    legacy = home / "weft" / "provider-config.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text('{"scope": "profile-x"}')
    target = home / "aletheia" / "provider-config.json"

    assert migrate_legacy_config(home, target) is True
    assert target.read_text() == '{"scope": "profile-x"}'
    assert not legacy.exists()
    assert migrate_legacy_config(home, target) is False


def test_legacy_cli_store_moves_to_the_named_path(tmp_path, monkeypatch):
    fake_home = tmp_path / "user"
    legacy = fake_home / ".weft" / "weft.sqlite3"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"cli records")
    monkeypatch.setattr("aletheia.paths.Path.home", classmethod(lambda cls: fake_home))
    target = fake_home / ".aletheia" / "aletheia.sqlite3"

    assert migrate_legacy_cli_store(target) is True
    assert target.read_bytes() == b"cli records"
    assert not legacy.exists()
