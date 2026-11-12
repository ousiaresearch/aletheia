"""The provider's runtime requirements: what they are, where they are declared, who has them.

Hermes installs a plugin's Python requirements from its *declaration*: ``pyproject.toml``'s
``[project.dependencies]`` when the plugin ships one, and ``plugin.yaml``'s ``python_dependencies``
only when it does not (``pm/plugin_declarations.read_python_declaration``). A requirement that lives
only in ``[project.optional-dependencies]`` is therefore never installed when an environment is
rebuilt, and the provider then fails closed at startup with no embedder — a gateway that comes up
clean and writes no memory.

Three separate questions, answered separately, because conflating them is how the first version of
this module lied:

* **Declaration** — does ``[project.dependencies]`` name the requirement with a marker that applies
  to the target platform? A requirement marked ``win32`` does not install on an arm64 Mac, and
  counting it as declared is exactly the regression this module exists to catch.
* **Presence** — can the interpreter that runs the gateway *import* the module? ``find_spec`` says
  a namespace directory exists; it does not execute ``__init__``, so it cannot see a wrong-arch
  wheel or a half-written install. The probe imports, isolated, from a neutral working directory.
* **Readiness** — does the checkpoint load and produce a vector? That is the provider's own
  ``is_available()``, run by ``scripts/check_runtime_deps.py --deep``. Presence is not readiness.

Nothing here raises for a condition it was built to report: unknown is an answer.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

#: import name -> the distribution that provides it. Distribution names are what requirements and
#: repair commands must name; import names are what a probe can actually check.
MODULE_DISTRIBUTIONS: dict[str, str] = {
    "mlx": "mlx",
    "mlx_embeddings": "mlx-embeddings",
    "sentence_transformers": "sentence-transformers",
}

#: modules that only apply on one platform: ``module -> (sys_platform, machine)``. A module absent
#: from this map is required everywhere. All three are Apple Silicon only, because the default
#: embedder and the reranker are the provider's contract and it runs nowhere else: installing them
#: on Linux would pull torch for a provider that fails closed there by design.
PLATFORM_ONLY: dict[str, tuple[str, str]] = {
    "mlx": ("darwin", "arm64"),
    "mlx_embeddings": ("darwin", "arm64"),
    "sentence_transformers": ("darwin", "arm64"),
}

#: The plugin ships in this repository, so the declaration is one directory above the package.
REPO_ROOT = Path(__file__).resolve().parent.parent

_PROBE_SENTINEL = "__ALETHEIA_PROBE__"


#: Marker variables this module can answer for a target other than its own process. A marker that
#: reads anything else cannot be evaluated for that target, and is reported unknown rather than
#: evaluated against the checker's interpreter — which is how a `python_version` marker silently
#: became the checker's Python rather than the environment's.
#: Submodule that proves a namespace-style package is really installed. ``mlx`` ships as a
#: namespace directory of extension modules; an ``mlx/`` folder with nothing importable inside is
#: not an install, and both import without error at the top level.
_NAMESPACE_CANARIES: dict[str, str] = {
    "mlx": "mlx.core",
    "mlx_embeddings": "mlx_embeddings.utils",
}

_SUPPLIED_MARKER_VARS = frozenset({
    "sys_platform", "platform_machine", "platform_system", "python_version", "python_full_version",
})


@dataclass(frozen=True)
class Target:
    """The platform whose requirements are being judged — the *target*, never the caller."""

    system: str
    machine: str
    python_version: str = ""

    def __str__(self) -> str:
        base = f"{self.system}/{self.machine}"
        return f"{base}/py{self.python_version}" if self.python_version else base


def local_target() -> Target:
    return Target(system=sys.platform, machine=platform.machine().lower(),
                  python_version=f"{sys.version_info.major}.{sys.version_info.minor}")


def required_modules(target: Target | None = None) -> dict[str, str]:
    """The ``{module: distribution}`` set that must be importable on ``target``."""
    target = local_target() if target is None else target
    required: dict[str, str] = {}
    for module, distribution in MODULE_DISTRIBUTIONS.items():
        expected = PLATFORM_ONLY.get(module)
        if expected is not None and (target.system, target.machine) != expected:
            continue
        required[module] = distribution
    return required


# --------------------------------------------------------------------------- declaration


def _canonical(name: str) -> str:
    """PEP 503 normalisation: what makes ``sentence_transformers`` and ``sentence-transformers``
    the same distribution."""
    return name.strip().lower().replace("_", "-").replace(".", "-")


def pyproject_path(pyproject: Path | str | None = None) -> Path:
    return REPO_ROOT / "pyproject.toml" if pyproject is None else Path(pyproject)


def declared_requirements(pyproject: Path | str | None = None) -> list[str] | None:
    """``[project.dependencies]`` as written, or ``None`` when there is no declaration.

    ``None`` is a real answer: an installed copy of this package carries no ``pyproject.toml``, and
    a checker running there must say "unknown" — not raise, and not claim the requirements are
    undeclared.
    """
    try:
        document = tomllib.loads(pyproject_path(pyproject).read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    specs = document.get("project", {}).get("dependencies", [])
    if not isinstance(specs, list):
        return None
    return [spec for spec in specs if isinstance(spec, str)]


def parser_available() -> bool:
    """Whether PEP 508 parsing is available at all.

    Without ``packaging`` every requirement looks unparseable, which would read as "the declaration
    is incomplete" — a false diagnosis with an unpinned repair. Unknown is the honest answer.
    """
    try:
        import packaging.requirements  # noqa: F401
    except ImportError:
        return False
    return True


def parse_requirement(spec: str):
    """A ``packaging`` Requirement, or ``None`` when the specifier cannot be parsed.

    Unparseable is unknown, never "missing": a dependency pinned to a URL or written in a legacy
    form must not read as absent and send the operator to install a different artifact.
    """
    try:
        from packaging.requirements import Requirement
    except ImportError:  # pragma: no cover - packaging ships with the build toolchain
        return None
    try:
        return Requirement(spec)
    except Exception:
        return None


#: Every marker variable PEP 508 defines. A closed set, so the question "which variables does this
#: marker read" can be answered by looking for these names rather than by walking packaging's
#: private parse tree (which is not an exported API and changes between versions).
_MARKER_VARIABLES = (
    "python_version", "python_full_version", "os_name", "sys_platform", "platform_release",
    "platform_system", "platform_version", "platform_machine", "platform_python_implementation",
    "implementation_name", "implementation_version", "extra",
)


def marker_variables(spec: str) -> set[str]:
    """The marker variable names a requirement tests, whatever the form of the marker."""
    requirement = parse_requirement(spec)
    if requirement is None or requirement.marker is None:
        return set()
    text = str(requirement.marker)
    return {name for name in _MARKER_VARIABLES if re.search(rf"\b{name}\b", text)}


def requirement_applies(spec: str, target: Target) -> bool | None:
    """Whether a requirement's marker admits ``target``.

    ``None`` — unknown — when the spec cannot be parsed, or when its marker tests a variable this
    module cannot supply for a target that is not the running process (``os_name``,
    ``platform_release``, the implementation). Answering those from the checker's own interpreter is
    how a marker about the *environment* silently became a statement about the *checker*.
    """
    requirement = parse_requirement(spec)
    if requirement is None:
        return None
    if requirement.marker is None:
        return True
    unsupported = marker_variables(spec) - _SUPPLIED_MARKER_VARS
    if unsupported and target != local_target():
        return None
    version = target.python_version or f"{sys.version_info.major}.{sys.version_info.minor}"
    try:
        return bool(requirement.marker.evaluate({
            "sys_platform": target.system,
            "platform_machine": target.machine,
            "platform_system": "Darwin" if target.system == "darwin" else target.system,
            "python_version": version,
            "python_full_version": f"{version}.0",
        }))
    except Exception:
        return None


def applicable_declarations(pyproject: Path | str | None = None,
                            target: Target | None = None) -> dict[str, str] | None:
    """Canonical distribution -> declared spec, for requirements that install on ``target``."""
    target = local_target() if target is None else target
    specs = declared_requirements(pyproject)
    if specs is None:
        return None
    if not parser_available():
        return None
    applicable: dict[str, str] = {}
    for spec in specs:
        requirement = parse_requirement(spec)
        if requirement is None:
            continue
        if requirement_applies(spec, target) is not True:
            continue
        applicable[_canonical(requirement.name)] = spec
    return applicable


def missing_declarations(pyproject: Path | str | None = None,
                         target: Target | None = None) -> list[str] | None:
    """Distributions the provider needs that the declaration does not install on ``target``.

    ``[]`` means an environment built from the declaration on ``target`` gets them. ``None`` means
    there is no declaration to read. A requirement present but marked for another platform counts
    as missing here — that marker is precisely what makes it never install.
    """
    target = local_target() if target is None else target
    applicable = applicable_declarations(pyproject, target)
    if applicable is None:
        return None
    return [distribution for distribution in required_modules(target).values()
            if _canonical(distribution) not in applicable]


def _quote(spec: str) -> str:
    """Shell-quote a requirement so the printed command is both valid and readable.

    ``shlex.quote`` wraps a spec containing single quotes in the ``'"'"'`` dance; a spec that has
    single quotes and no double quotes reads better inside double quotes, which the shell also
    accepts — and markers always look like that.
    """
    if "'" in spec and '"' not in spec and "$" not in spec and "`" not in spec and "\\" not in spec:
        return f'"{spec}"'
    return shlex.quote(spec)


def find_uv() -> Path | None:
    """uv if this machine has one — Hermes installs it under ``$HERMES_HOME/bin``."""
    candidates = [Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))) / "bin" / "uv"]
    on_path = shutil.which("uv")
    if on_path:
        candidates.append(Path(on_path))
    return next((path for path in candidates if path.is_file()), None)


def install_command(python: Path | str, distributions: list[str], pyproject: Path | str | None = None,
                    *, uv: Path | str | None = None) -> str:
    """The command that installs ``distributions`` into the environment at ``python``.

    Hermes builds environments with uv and they carry no pip, so ``python -m pip install`` is a
    command that fails the moment it is pasted. Prefer uv; fall back to pip only when this
    interpreter actually has it.
    """
    specs = applicable_declarations(pyproject) or {}
    wanted = [specs.get(_canonical(name), name) for name in distributions]
    quoted = " ".join(_quote(spec) for spec in wanted)
    resolved = Path(uv) if uv is not None else find_uv()
    if resolved is not None:
        return f"{resolved} pip install --python {python} {quoted}"
    return f"{python} -m pip install {quoted}"


# --------------------------------------------------------------------------- environment


@dataclass
class Environment:
    """The environment the installer resolved, and anything about that resolution worth saying."""

    venv: Path | None = None
    facts: Path | None = None
    note: str = ""

    @property
    def python(self) -> Path | None:
        return None if self.venv is None else self.venv / "bin" / "python"


def _environment_ids(document: dict) -> tuple[str, str]:
    """(the env named by ``environment``, the env holding ``resolved_lock``), as short ids."""
    venv = document.get("packages", {}).get("venv", {}) if isinstance(document, dict) else {}
    named = str(venv.get("environment") or "")
    lock = str(venv.get("resolved_lock") or "")
    named_id = Path(named).parent.name if named else ""
    lock_id = lock.split("/environments/", 1)[1].split("/", 1)[0] if "/environments/" in lock else ""
    return named_id[:8], lock_id[:8]


def active_environment(hermes_home: Path | str) -> Environment:
    """The venv the installer resolved facts.json to — the one the gateway starts against.

    ``venv`` is ``None`` when no install facts name an existing environment: unknown, never guessed
    from whichever directory sorts first. ``note`` records a disagreement between the named
    environment and the environment holding the resolved lock, because those have pointed at
    different venvs on this machine and only one of them had the models.
    """
    home = Path(hermes_home).expanduser()
    candidates = sorted(home.glob("installs/*/facts.json"),
                        key=lambda path: path.stat().st_mtime, reverse=True)
    for facts in candidates:
        try:
            document = json.loads(facts.read_text(encoding="utf-8"))
            venv = Path(document["packages"]["venv"]["environment"]).expanduser()
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if not venv.is_dir():
            continue
        named_id, lock_id = _environment_ids(document)
        note = ""
        if lock_id and named_id and lock_id != named_id:
            note = (f"facts.json names environment {named_id} but resolves the lock in {lock_id}; "
                    f"the named environment is the one used here")
        return Environment(venv=venv, facts=facts, note=note)
    return Environment()


# --------------------------------------------------------------------------- presence


#: For a package that imports as a namespace (no ``__file__`` of its own), the submodule that
#: proves it is installed: ``mlx`` legitimately ships as a namespace directory holding extension
#: modules (``mlx/core*.so``), so "no ``__file__``" alone cannot mean absent — but an empty
#: ``mlx/`` folder can. None means the top-level package is expected to have a file.
_PROBE = r'''
import importlib, json, platform, sys
requested = json.loads(sys.argv[1])
canaries = json.loads(sys.argv[2])
modules = {}
for name in requested:
    try:
        module = __import__(name)
        if getattr(module, "__file__", None) is not None:
            modules[name] = {"present": True, "error": ""}
            continue
        canary = canaries.get(name)
        if not canary:
            modules[name] = {"present": False, "error": "no module file and no canary to prove it"}
            continue
        try:
            importlib.import_module(canary)
            modules[name] = {"present": True, "error": ""}
        except BaseException as exc:
            modules[name] = {"present": False,
                             "error": "namespace directory with no usable submodule: " +
                                      (type(exc).__name__ + ": " + str(exc))[:200]}
    except BaseException as exc:
        modules[name] = {"present": False, "error": (type(exc).__name__ + ": " + str(exc))[:300]}
platform_info = {"system": sys.platform, "machine": platform.machine().lower(),
                 "python_version": "%d.%d" % sys.version_info[:2]}
print("__ALETHEIA_PROBE__" + json.dumps({"modules": modules, "platform": platform_info}))
'''


@dataclass
class ProbeResult:
    """What an interpreter could and could not import, and why the probe could not run."""

    python: str = ""
    target: Target | None = None
    found: dict[str, bool] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    error: str = ""

    @property
    def missing(self) -> list[str]:
        return [module for module, present in self.found.items() if not present]


def probe(python: Path | str, modules: dict[str, str] | None = None, *, target: Target | None = None,
          timeout: float = 300.0, cwd: Path | str | None = None) -> ProbeResult:
    """Import each module in ``python``, isolated from this process's environment and cwd.

    ``-I`` drops ``PYTHONPATH``, ``PYTHONHOME`` and the user site; the child runs in a scratch
    directory so an ``mlx/`` folder in the caller's working directory cannot answer for an install.
    The import is real rather than a ``find_spec`` lookup: a wrong-arch wheel and a broken
    ``__init__`` both have a spec, and neither can be imported.
    """
    # Ask for every module this package knows about, not the caller's platform subset: the child
    # reports its own platform, and only then can the required set be judged for the *target*. Asking
    # for the caller's subset is how a Linux checker blessed a Darwin environment and a Darwin
    # checker demanded mlx of a Linux one.
    wanted = dict(MODULE_DISTRIBUTIONS) if modules is None else modules
    canaries = {name: _NAMESPACE_CANARIES.get(name) for name in wanted}
    result = ProbeResult(python=str(python), target=target)
    interpreter = Path(str(python))
    if not interpreter.is_file():
        result.error = f"no interpreter at {interpreter}"
        return result
    if cwd is not None:
        completed = _run_probe(interpreter, wanted, canaries, Path(cwd), timeout)
    else:
        with tempfile.TemporaryDirectory(prefix="aletheia-probe-") as scratch:
            completed = _run_probe(interpreter, wanted, canaries, Path(scratch), timeout)
    if isinstance(completed, str):
        result.error = completed
        return result
    payload = None
    for line in reversed((completed.stdout or "").splitlines()):
        if line.startswith(_PROBE_SENTINEL):
            try:
                payload = json.loads(line[len(_PROBE_SENTINEL):])
            except ValueError:
                payload = None
            break
    if not isinstance(payload, dict):
        tail = (completed.stderr or "").strip().splitlines()[-1:] or [""]
        result.error = f"probe produced no result (exit {completed.returncode}): {tail[0][:300]}"
        return result
    reported = payload.get("platform") or {}
    if reported:
        result.target = Target(system=str(reported.get("system") or ""),
                               machine=str(reported.get("machine") or ""),
                               python_version=str(reported.get("python_version") or ""))
    for module, entry in (payload.get("modules") or {}).items():
        present = bool(entry.get("present"))
        result.found[str(module)] = present
        if not present:
            result.errors[str(module)] = str(entry.get("error") or "")
    # A child that answered about fewer modules than it was asked about must not read as a pass for
    # the ones it skipped: absence of an answer is absence of evidence.
    for module in wanted:
        if module not in result.found:
            result.found[module] = False
            result.errors[module] = "probe did not report this module"
    return result


def _run_probe(interpreter: Path, wanted: dict[str, str], canaries: dict[str, str | None],
               workdir: Path, timeout: float):
    """Run the probe, returning the completed process or a failure message."""
    try:
        return subprocess.run(
            [str(interpreter), "-I", "-c", _PROBE, json.dumps(sorted(wanted)), json.dumps(canaries)],
            capture_output=True, text=True, errors="replace", timeout=timeout, check=False,
            cwd=str(workdir),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"probe failed: {exc.__class__.__name__}: {exc}"


def missing_modules(result: ProbeResult, target: Target | None = None) -> list[str]:
    """The required modules this probe could not import, judged for the probe's own platform.

    ``required_modules`` is empty off Apple Silicon, and an empty requirement set must never be
    reported as "all present" — that is a silent pass for a platform the provider cannot run on.
    Callers distinguish "nothing to check" from "everything checks out" with
    :func:`provider_supported`.
    """
    target = target or result.target or local_target()
    return [module for module in required_modules(target) if not result.found.get(module)]


def provider_supported(target: Target | None = None) -> bool:
    """Whether this package's provider can run at all on ``target``."""
    return bool(required_modules(local_target() if target is None else target))


def environment_probe(hermes_home: Path | str, modules: dict[str, str] | None = None) -> ProbeResult:
    """Probe the interpreter of the environment facts.json resolves to."""
    environment = active_environment(hermes_home)
    if environment.python is None:
        result = ProbeResult()
        result.error = "no resolved environment in install facts"
        return result
    return probe(environment.python, modules)