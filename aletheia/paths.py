"""Where a profile's store lives, and how a pre-rename (Weft) layout is carried forward.

The product was called Weft before 2.0.0. Its defaults were ``<profile>/weft/weft.sqlite3`` for the
Hermes provider and ``~/.weft/weft.sqlite3`` for the CLI, and a profile's provider settings sat in
``<profile>/weft/provider-config.json``. Those paths are moved once, on first use, so the rename
costs nobody their memory; nothing is deleted if the move cannot be made, and a store that already
exists at the new path always wins.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

LEGACY_DIR = "weft"
LEGACY_DB_NAME = "weft.sqlite3"


def legacy_store_for(db_path: Path) -> Path:
    """The pre-rename store that belongs to ``db_path``'s directory family."""
    return db_path.parent.parent / LEGACY_DIR / LEGACY_DB_NAME


def migrate_legacy_store(db_path: Path) -> bool:
    """Move ``<profile>/weft/weft.sqlite3`` (with its WAL) to ``db_path`` once. True if moved."""
    db_path = Path(db_path).expanduser()
    legacy = legacy_store_for(db_path)
    if db_path.exists() or not legacy.is_file():
        return False
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        for suffix in ("", "-wal", "-shm"):
            source = Path(str(legacy) + suffix)
            if source.is_file():
                os.replace(source, Path(str(db_path) + suffix))
    except OSError as exc:  # a read-only or foreign-owned legacy store must not stop startup
        logger.warning("aletheia: legacy store not moved (%s); starting at %s", exc, db_path)
        return False
    logger.info("aletheia: moved the pre-rename store %s to %s", legacy, db_path)
    return True


def migrate_legacy_config(hermes_home: Path, config_path: Path) -> bool:
    """Move ``<profile>/weft/provider-config.json`` beside the new store once."""
    hermes_home, config_path = Path(hermes_home).expanduser(), Path(config_path).expanduser()
    legacy = hermes_home / LEGACY_DIR / "provider-config.json"
    if config_path.exists() or not legacy.is_file():
        return False
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.replace(legacy, config_path)
    except OSError as exc:
        logger.warning("aletheia: legacy provider config not moved (%s)", exc)
        return False
    logger.info("aletheia: moved the pre-rename provider config %s to %s", legacy, config_path)
    return True


def migrate_legacy_cli_store(db_path: Path) -> bool:
    """The CLI's ``~/.weft/weft.sqlite3`` is the same family: one store per home directory."""
    db_path = Path(db_path).expanduser()
    legacy = Path.home() / ".weft" / LEGACY_DB_NAME
    if db_path.exists() or not legacy.is_file() or legacy == db_path:
        return False
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        for suffix in ("", "-wal", "-shm"):
            source = Path(str(legacy) + suffix)
            if source.is_file():
                os.replace(source, Path(str(db_path) + suffix))
    except OSError as exc:
        logger.warning("aletheia: legacy CLI store not moved (%s); starting at %s", exc, db_path)
        return False
    logger.info("aletheia: moved the pre-rename CLI store %s to %s", legacy, db_path)
    return True
