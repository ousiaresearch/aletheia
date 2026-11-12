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
import json
import os
import re
from pathlib import Path

SCOPE_PREFIX = "profile:"
SHARED_SCOPE = "shared"
DEFAULT_PROFILE = "default"
POLICY_FILE = "policy.json"

_NAME_RE = re.compile(r"[^a-z0-9_-]+")
_SCOPE_RE = re.compile(r"^(?:profile:[a-z0-9_-]+|project:[a-z0-9_-]+|shared)$")


def normalise_name(value: object) -> str:
    """Lowercase, collapse unsafe runs to ``-``, and keep distinct inputs distinct.

    Cleaning is many-to-one on its own (``foo bar``, ``foo.bar`` and ``foo-bar`` all become
    ``foo-bar``), and truncation joins names that agree for their first characters. Either would let
    two profiles share one scope, so when cleaning or truncation changes the value a short digest of
    the original is appended: distinct names cannot collide, and the readable prefix stays.
    """
    raw = str(value or "").strip().lower()
    cleaned = _NAME_RE.sub("-", raw).strip("-")
    short = cleaned[:48].strip("-")
    if short == raw:
        return short
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    return f"{short}-{digest}" if short else f"n-{digest}"


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


# ---------------------------------------------------------------------------------------------
# Grant authority
#
# A grant read from the profile's own config is self-service: any process that can write inside the
# profile can give itself every other profile's memory. The authority therefore lives in a host-
# owned file beside the store, readable only by its owner. A profile's config may *request* a scope;
# the policy decides. No policy file means no cross-profile access at all.


def policy_path(hermes_home: str | Path) -> Path:
    return hermes_root(hermes_home) / "aletheia" / POLICY_FILE


def load_policy(hermes_home: str | Path) -> dict:
    """Read the host grant policy. A missing, foreign-owned or loosely-permissioned file grants nothing."""
    path = policy_path(hermes_home)
    result: dict = {"present": False, "profiles": {}, "error": "", "path": str(path)}
    if not path.is_file():
        return result
    result["present"] = True
    try:
        stat = path.stat()
        if stat.st_uid != os.getuid():
            result["error"] = "policy file is not owned by this user"
            return result
        if stat.st_mode & 0o077:
            result["error"] = "policy file is readable or writable by group/other; refusing to honour it"
            return result
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        result["error"] = f"policy unreadable: {exc}"
        return result
    profiles = data.get("profiles") if isinstance(data, dict) else None
    if not isinstance(profiles, dict):
        result["error"] = "policy has no profiles map"
        return result
    result["profiles"] = {str(k): v for k, v in profiles.items() if isinstance(v, dict)}
    return result


def policy_entry(policy: dict, profile: str) -> dict:
    """This profile's entry, or an empty one (no entry grants nothing)."""
    return dict((policy.get("profiles") or {}).get(profile) or {})


def effective_read_grants(own_scope: str, requested: dict, entry: dict,
                          policy: dict) -> tuple[str, ...]:
    """Own scope plus whatever the host policy allows the profile to read.

    A requested scope must be named by the policy; a request the policy does not name is ignored
    (and the caller warns). ``shared`` is granted by the policy's own flag, not by asking.
    """
    grants = [own_scope]
    if not policy.get("present") or policy.get("error"):
        return tuple(grants)
    allowed = set(parse_scopes(entry.get("read_scopes")))
    for scope in parse_scopes(requested.get("read_scopes")):
        if scope in allowed and scope not in grants:
            grants.append(scope)
    if _truthy(entry.get("shared")) and SHARED_SCOPE not in grants:
        grants.append(SHARED_SCOPE)
    return tuple(grants)


def effective_write_grants(own_scope: str, entry: dict, policy: dict) -> tuple[str, ...]:
    """Own scope, plus ``shared`` only when the host policy says this profile may write there."""
    grants = [own_scope]
    if not policy.get("present") or policy.get("error"):
        return tuple(grants)
    if _truthy(entry.get("shared")) and _truthy(entry.get("shared_write")):
        grants.append(SHARED_SCOPE)
    return tuple(grants)


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def as_bool(value: object) -> bool:
    """Public spelling of the config truthiness rule (``"false"`` is False, not a truthy string)."""
    return _truthy(value)