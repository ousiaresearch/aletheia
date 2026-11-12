#!/usr/bin/env python3
"""Check that what the gateway runs can import — and actually load — the provider's models.

A rebuilt environment that lacks the embedder does not fail loudly: the gateway starts, the provider
reports itself unavailable, and external memory is silently off for every session. This script makes
that a named, exit-coded failure, and prints a repair command that works in an environment with no
pip.

    python scripts/check_runtime_deps.py                 # presence, in the resolved environment
    python scripts/check_runtime_deps.py --deep          # also load the embedder and the reranker
    python scripts/check_runtime_deps.py --env PATH/venv # a specific environment

Exit codes: 0 every requirement present (and ready, with --deep); 1 a requirement is missing, the
declaration does not install it, or readiness failed; 2 the environment or interpreter could not be
determined — unknown, which is not the same as healthy.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_MODULE_NAME = "_aletheia_runtime_deps"


def _load_runtime_deps(repo: Path):
    """Load this repository's copy by path.

    Importing ``aletheia.runtime_deps`` would resolve through whatever ``aletheia`` this interpreter
    happens to see — an installed dist, another checkout, an editable install pointing elsewhere —
    and then a check about *this* repository would be answered by a different one.
    """
    path = Path(repo) / "aletheia" / "runtime_deps.py"
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"no importable runtime_deps at {path}")
    module = importlib.util.module_from_spec(spec)
    # A module must be in sys.modules while its body runs: @dataclass resolves the class's module
    # through sys.modules, and a missing entry fails with "'NoneType' object has no attribute
    # '__dict__'" instead of anything that names the real problem.
    sys.modules[_MODULE_NAME] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(_MODULE_NAME, None)
        raise
    return module


_READY = r'''
import json
try:
    from aletheia.providers.hermes import AletheiaMemoryProvider as Provider
    provider = Provider()
    available = provider.is_available()
    print("__ALETHEIA_READY__" + json.dumps(
        {"available": available, "reason": "" if available else provider.unavailable_reason()}))
except BaseException as exc:
    print("__ALETHEIA_READY__" + json.dumps(
        {"available": False, "reason": (type(exc).__name__ + ": " + str(exc))[:600]}))
'''


def _plugin_source(environment: Path | None, hermes_home: Path, repo: Path) -> tuple[Path, str]:
    """The copy of the package to exercise, and a plain statement of which one that is.

    ``workspace/plugin-sources`` sits beside the venv, not inside it — globbing it from the venv
    silently falls back to the checkout and then attests to code the gateway is not running.
    """
    candidates: list[tuple[Path, str]] = []
    if environment is not None:
        candidates.append((environment.parent / "workspace" / "plugin-sources", "environment"))
    candidates.append((hermes_home / "plugins" / "aletheia", "plugins dir"))
    candidates.append((repo, "checkout"))
    for base, label in candidates:
        if label == "environment":
            for source in sorted(base.glob("aletheia-*")):
                if (source / "aletheia" / "__init__.py").is_file():
                    return source, "environment plugin source"
        elif (base / "aletheia" / "__init__.py").is_file():
            return base, label
    return repo, "checkout (no plugin source found)"


def _readiness(python: Path, source: Path) -> dict:
    """Load both models in the target interpreter — the deep check, and the only one that is readiness."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(source)
    try:
        completed = subprocess.run([str(python), "-c", _READY], capture_output=True, text=True,
                                   errors="replace", timeout=900, check=False, env=env,
                                   cwd=str(source))
    except (OSError, subprocess.SubprocessError) as exc:
        return {"available": False, "reason": f"readiness probe failed: {exc.__class__.__name__}: {exc}"}
    for line in reversed((completed.stdout or "").splitlines()):
        if line.startswith("__ALETHEIA_READY__"):
            try:
                return json.loads(line[len("__ALETHEIA_READY__"):])
            except ValueError:
                break
    tail = (completed.stderr or "").strip().splitlines()[-1:] or [""]
    return {"available": False, "reason": f"readiness produced no result (exit {completed.returncode}): {tail[0][:300]}"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--hermes-home", default=os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
    parser.add_argument("--env", default=None, help="an environment's venv directory (default: the resolved one)")
    parser.add_argument("--repo", default=str(REPO), help="the checkout whose declaration is judged")
    parser.add_argument("--deep", action="store_true", help="also load the embedder and the reranker")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    runtime_deps = None
    try:
        runtime_deps = _load_runtime_deps(Path(args.repo))
    except Exception as exc:  # a checker that cannot load is unknown, not a pass
        message = f"cannot load runtime_deps from {args.repo}: {exc.__class__.__name__}: {exc}"
        print(json.dumps({"error": message}) if args.json else f"unknown: {message}")
        return 2

    hermes_home = Path(args.hermes_home).expanduser()
    if args.env:
        environment = Path(args.env).expanduser()
        note = ""
        if not environment.is_dir():
            message = f"no environment at {environment}"
            print(json.dumps({"error": message}) if args.json else f"unknown: {message}")
            return 2
    else:
        resolved = runtime_deps.active_environment(hermes_home)
        environment, note = resolved.venv, resolved.note
        if environment is None:
            message = f"no resolved environment under {hermes_home}/installs/*/facts.json"
            print(json.dumps({"error": message}) if args.json else f"unknown: {message}")
            return 2

    python = environment / "bin" / "python"
    result = runtime_deps.probe(python)
    target = result.target or runtime_deps.local_target()
    required = runtime_deps.required_modules(target)
    supported = runtime_deps.provider_supported(target)
    missing_modules = runtime_deps.missing_modules(result, target)
    undeclared = runtime_deps.missing_declarations(Path(args.repo) / "pyproject.toml", target)

    report: dict = {
        "environment": str(environment),
        "python": str(python),
        "target": str(target),
        "required": required,
        "supported": supported,
        "present": {module: present for module, present in result.found.items()},
        "missing_modules": missing_modules,
        "import_errors": result.errors,
        # Distribution names, so a repair is unambiguous: sentence_transformers is a module,
        # sentence-transformers is what gets installed.
        "missing": [runtime_deps.MODULE_DISTRIBUTIONS[m] for m in missing_modules
                    if m in runtime_deps.MODULE_DISTRIBUTIONS],
        "declaration": ("unknown" if undeclared is None
                        else "incomplete" if undeclared else "ok"),
        "undeclared": undeclared,
        "probe_error": result.error,
        "note": note,
    }
    if args.env:
        report["note"] = "environment supplied by --env"
    if args.deep:
        source, source_label = _plugin_source(environment, hermes_home, Path(args.repo))
        report["readiness"] = _readiness(python, source)
        report["source"] = {"path": str(source), "kind": source_label}

    # Everything that needed the loaded module is done; leaving it registered means a caller that
    # later imports `aletheia.runtime_deps` holds two distinct Target classes.
    sys.modules.pop(_MODULE_NAME, None)

    # Three ways to learn nothing, all of them unknown rather than healthy: the probe could not run,
    # the declaration could not be read, or this platform cannot run the provider at all.
    unknown = bool(report["probe_error"]) or undeclared is None or not supported
    failed = bool(missing_modules) or bool(undeclared)
    if args.deep and not report.get("readiness", {}).get("available"):
        failed = True

    if args.json:
        print(json.dumps(report, indent=2))
        return 2 if unknown else (1 if failed else 0)

    print(f"environment: {environment}")
    print(f"python:      {python}")
    print(f"target:      {target}")
    if note:
        print(f"note:        {note}")
    if not supported:
        print(f"  NOT JUDGED the provider does not run on {target}; nothing is required there and")
        print("             nothing was checked — this is not a pass")
    for module, distribution in report["required"].items():
        present = report["present"].get(module)
        mark = "ok" if present else "MISSING"
        print(f"  {mark:<8} {module:<24} ({distribution})")
        if not present and report["import_errors"].get(module):
            print(f"           {report['import_errors'][module]}")
    if report["declaration"] == "incomplete":
        print(f"  UNDECLARED {', '.join(undeclared)} — [project.dependencies] does not install them on {target}")
        print("             an environment rebuilt from the declaration would come up without them")
        print("             fix the declaration; this is a source edit, not an install")
    elif report["declaration"] == "unknown":
        print("  UNKNOWN    no pyproject.toml in the checkout — declaration not judged")
    if report["probe_error"]:
        print(f"  UNKNOWN    {report['probe_error']}")
    if args.deep:
        readiness = report["readiness"]
        print(f"  source     {report['source']['kind']}: {report['source']['path']}")
        if readiness.get("available"):
            print("  ready      embedder and reranker loaded and probed")
        else:
            print(f"  NOT READY  {str(readiness.get('reason') or 'readiness unknown')[:300]}")

    if unknown:
        if not supported:
            print(f"\nunknown: the provider cannot run on {target}, so this environment proves nothing")
        elif undeclared is None:
            print("\nunknown: the declaration could not be read, so it was not judged")
        else:
            print("\nunknown: the environment could not be probed; treat this as unsolved, not as healthy")
        return 2
    if failed:
        if report["missing"]:
            print("\nrepair (into the environment that lacks them):")
            print(f"  {runtime_deps.install_command(python, report['missing'], Path(args.repo) / 'pyproject.toml')}")
        if args.deep and not report["readiness"].get("available"):
            print("\nthe environment is not serving this provider; check facts.json before repairing")
        return 1
    print("\nall requirements present")
    return 0


if __name__ == "__main__":
    sys.exit(main())