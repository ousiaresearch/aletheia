"""Profile identity → scope name, and the store path a host shares across profiles.

Hermes hands a memory provider its profile home (``hermes_home``) and, since the multiplexed
gateway serves several profiles from one process, the profile's own name (``agent_identity``).
Identity must come from that name, never from the last path segment: the default profile's home is
``~/.hermes``, whose basename turned into the scope ``profile-.hermes``, and any two homes ending in
the same directory name collided.

Scope grammar (ADR-2):

* ``profile:<name>`` — one Hermes profile. ``default`` for the root home.
* ``project:<slug>`` — an explicit work scope inside one profile.
* ``shared`` — cross-profile knowledge, opt-in per profile.

The store is host-level: ``<hermes_root>/aletheia/aletheia.sqlite3`` where ``hermes_root`` is the
directory holding ``profiles/`` (or the default home itself). Profiles are scopes inside that one
file, not separate files.
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

SCOPE_PREFIX = "profile:"
SHARED_SCOPE = "shared"
DEFAULT_PROFILE = "default"

_NAME_RE = re.compile(r"[^a-z0-9_-]+")
_SCOPE_RE = re.compile(r"^(?:profile:[a-z0-9_-]+|project:[a-z0-9_-]+|shared)$")


def normalise_name(value: object) -> str:
    """Lowercase, collapse unsafe runs to ``-``, trim. Empty string when nothing usable remains."""
    cleaned = _NAME_RE.sub("-", str(value or "").strip().lower()).strip("-")
    return cleaned[:64]


def _default_root(default_root: str | Path | None) -> Path:
    if default_root is not None:
        return Path(default_root).expanduser()
    configured = os.environ.get("HERMES_DEFAULT_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".hermes"


def profile_name(hermes_home: str | Path, agent_identity: object = None, *,
                 default_root: str | Path | None = None) -> str:
    """The profile's canonical name.

    ``agent_identity`` wins when Hermes supplies one (``get_active_profile_name()``). Otherwise the
    path decides: the default root is ``default``, ``<root>/profiles/<name>`` is ``<name>``, and any
    other home is a stable ``custom-<hash>`` — never a basename that another home can share.
    """
    identity = normalise_name(agent_identity)
    if identity:
        return identity
    home = Path(hermes_home).expanduser()
    try:
        resolved = home.resolve()
    except OSError:
        resolved = home
    try:
        if resolved == _default_root(default_root).resolve():
            return DEFAULT_PROFILE
    except OSError:
        if str(resolved) == str(_default_root(default_root)):
            return DEFAULT_PROFILE
    if resolved.parent.name == "profiles":
        name = normalise_name(resolved.name)
        if name:
            return name
    return f"custom-{hashlib.sha256(str(resolved).encode('utf-8')).hexdigest()[:8]}"


def profile_scope(hermes_home: str | Path, agent_identity: object = None, *,
                  default_root: str | Path | None = None) -> str:
    """``profile:<name>`` for the given home."""
    return f"{SCOPE_PREFIX}{profile_name(hermes_home, agent_identity, default_root=default_root)}"


def hermes_root(hermes_home: str | Path) -> Path:
    """The directory that owns ``profiles/`` — the profile home itself for any other layout."""
    home = Path(hermes_home).expanduser()
    if home.parent.name == "profiles":
        return home.parent.parent
    return home


def default_store_path(hermes_home: str | Path) -> Path:
    """One host-level store, shared by every profile as a scope inside it."""
    return hermes_root(hermes_home) / "aletheia" / "aletheia.sqlite3"


def parse_scopes(value: object) -> tuple[str, ...]:
    """Read ``read_scopes`` from config: a list, or a comma/space separated string.

    A bare string is a common config shape; it must never be iterated character by character.
    Unknown names are dropped rather than trusted, and duplicates collapse.
    """
    if value is None:
        return ()
    if isinstance(value, str):
        raw = re.split(r"[,\s]+", value.strip())
    else:
        try:
            raw = [str(item) for item in value]  # type: ignore[union-attr]
        except TypeError:
            return ()
    seen: list[str] = []
    for item in raw:
        name = str(item or "").strip().lower()
        if _SCOPE_RE.match(name) and name not in seen:
            seen.append(name)
    return tuple(seen)


def read_grants(own_scope: str, config: dict) -> tuple[str, ...]:
    """Scopes this profile may read: its own, configured ``read_scopes``, and ``shared`` when opted in."""
    grants = [own_scope]
    for scope in parse_scopes(config.get("read_scopes")):
        if scope not in grants:
            grants.append(scope)
    if _truthy(config.get("shared")) and SHARED_SCOPE not in grants:
        grants.append(SHARED_SCOPE)
    return tuple(grants)


def write_grants(own_scope: str, config: dict) -> tuple[str, ...]:
    """Scopes this profile may write: its own, plus ``shared`` only with ``shared_write``."""
    grants = [own_scope]
    if _truthy(config.get("shared")) and _truthy(config.get("shared_write")):
        grants.append(SHARED_SCOPE)
    return tuple(grants)


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}