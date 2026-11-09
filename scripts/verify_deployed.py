#!/usr/bin/env python3
"""Report which Aletheia is deployed, and whether the deployed copy can actually run.

A plugin can exist in four places at once: the development checkout, the profile's ``plugins/``
directory, the plugin host's copied source tree, and an installed distribution in one of the
gateway environments. The gateway imports one of them. Reading the repository proves nothing about
the running process, so this script reports every copy it can find, then asks the environment the
installer resolved whether it can import the provider's models.

Three verdicts, not two:

* ``drift`` — the copies disagree, or a requirement the provider needs is absent from the
  environment, or the declaration does not install it. Exit 1.
* ``unknown`` — the environment or interpreter could not be determined, so the question was not
  answered. Exit 2. This is *not* a pass: the failure mode that motivated this check is a gateway
  that starts cleanly with no embedder, and a check that cannot look must not print "no drift".
* ``no drift`` — every copy matches and every requirement imports. Exit 0.

Usage:
    python scripts/verify_deployed.py [--repo PATH] [--hermes-home PATH] [--env PATH/venv] [--json]
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
from pathlib import Path

VERSION_RE = re.compile(r"""__version__\s*=\s*["']([^"']+)["']""")
_MODULE_NAME = "_aletheia_runtime_deps"


def _read_version(path: Path) -> str:
    try:
        match = VERSION_RE.search(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return ""
    return match.group(1) if match else ""


def _plugin_yaml_version(path: Path) -> str:
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip().startswith("version:"):
                return line.split(":", 1)[1].strip().strip("'\"")
    except OSError:
        return ""
    return ""


#: Every distribution name this project has shipped under. The installed one is ``ousia-aletheia``,
#: whose dist-info directory is ``ousia_aletheia-2.1.0.dist-info`` — a bare ``aletheia-*`` glob sees
#: nothing and reports the installed copy as absent, which is how a 2.0.1 environment read "no drift".
DISTRIBUTION_NAMES = frozenset({"aletheia", "ousia-aletheia"})


def _dist_versions(site_packages: Path) -> dict[str, str]:
    found: dict[str, str] = {}
    for entry in sorted(site_packages.glob("*aletheia*.dist-info")):
        name = version = ""
        try:
            for line in (entry / "METADATA").read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("Name:") and not name:
                    name = line.split(":", 1)[1].strip()
                elif line.startswith("Version:") and not version:
                    version = line.split(":", 1)[1].strip()
                if name and version:
                    break
        except OSError:
            continue
        if version and name.lower().replace("_", "-") in DISTRIBUTION_NAMES:
            found[str(entry)] = version
    return found


def _active_environment_id(hermes_home: Path, override: Path | None = None) -> str:
    """The eight-character id of the environment the installer resolved, or '' when unknown."""
    if override is not None:
        text = str(override)
        return text.split("/environments/", 1)[1].split("/", 1)[0][:8] if "/environments/" in text else ""
    candidates = sorted(Path(hermes_home).glob("installs/*/facts.json"),
                        key=lambda path: path.stat().st_mtime, reverse=True)
    for facts in candidates:
        try:
            document = json.loads(facts.read_text(encoding="utf-8"))
            environment = str(document["packages"]["venv"]["environment"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if not Path(environment).is_dir():
            continue
        if "/environments/" in environment:
            return environment.split("/environments/", 1)[1].split("/", 1)[0][:8]
        return ""
    return ""


def _load_runtime_deps(repo: Path):
    """This repository's ``runtime_deps``, loaded by path.

    Importing ``aletheia.runtime_deps`` would resolve through whatever ``aletheia`` this interpreter
    sees — an installed dist, an editable install pointing at another checkout — so a report about
    the repository under test could be answered by a different tree. Reported as a string on
    failure: a checker that cannot load must not raise, and must not read as a pass.
    """
    path = Path(repo) / "aletheia" / "runtime_deps.py"
    try:
        spec = importlib.util.spec_from_file_location(_MODULE_NAME, path)
        if spec is None or spec.loader is None:
            return None, f"no importable module at {path}"
        module = importlib.util.module_from_spec(spec)
        # The module must be in sys.modules while its body runs: @dataclass resolves its module
        # through sys.modules, and a missing entry fails with "'NoneType' object has no attribute
        # '__dict__'" instead of naming the real problem.
        sys.modules[_MODULE_NAME] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(_MODULE_NAME, None)
            raise
        return module, ""
    except Exception as exc:
        return None, f"{exc.__class__.__name__}: {exc}"


def runtime_deps_report(repo: Path, hermes_home: Path, environment_override: Path | None = None) -> dict:
    """Ask the resolved environment whether it can import the provider's requirements."""
    module, load_error = _load_runtime_deps(repo)
    if module is None:
        return {"environment": "", "verdict": "unknown", "error": f"runtime_deps unavailable: {load_error}"}
    try:
        note = ""
        if environment_override is not None:
            environment = environment_override
            if not environment.is_dir():
                return {"environment": str(environment), "verdict": "unknown",
                        "error": f"no environment at {environment}"}
            note = "environment supplied by --env"
        else:
            resolved = module.active_environment(hermes_home)
            environment, note = resolved.venv, resolved.note
            if environment is None:
                return {"environment": "", "verdict": "unknown",
                        "error": "no resolved environment in install facts"}
        python = environment / "bin" / "python"
        result = module.probe(python)
        target = result.target or module.local_target()
        supported = module.provider_supported(target)
        missing_modules = module.missing_modules(result, target)
        undeclared = module.missing_declarations(Path(repo) / "pyproject.toml", target)
        missing_distributions = [module.MODULE_DISTRIBUTIONS[m] for m in missing_modules
                                 if m in module.MODULE_DISTRIBUTIONS]
        # Unknown when nothing could be learned: no probe, no readable declaration, or a platform
        # the provider cannot run on — where an empty requirement set would otherwise read "clean".
        verdict = ("unknown" if (result.error or undeclared is None or not supported)
                   else ("drift" if (missing_modules or undeclared) else "ok"))
        return {
            "environment": str(environment),
            "python": result.python,
            "target": str(target),
            "supported": supported,
            "present": result.found,
            "import_errors": result.errors,
            "missing_modules": missing_modules,
            "missing": missing_distributions,
            "declaration": "unknown" if undeclared is None else ("incomplete" if undeclared else "ok"),
            "undeclared": undeclared,
            "repair": (module.install_command(python, missing_distributions, Path(repo) / "pyproject.toml")
                       if missing_distributions else ""),
            "error": result.error or ("" if supported else f"the provider cannot run on {target}; not judged")
                                 or ("" if undeclared is not None else "the declaration could not be read"),
            "note": note,
            "verdict": verdict,
        }
    except Exception as exc:  # a broken probe is unknown, not a pass
        return {"environment": "", "verdict": "unknown", "error": f"{exc.__class__.__name__}: {exc}"}


def collect(repo: Path, hermes_home: Path, environment_override: Path | None = None) -> dict:
    repo = Path(repo).expanduser()
    hermes_home = Path(hermes_home).expanduser()
    report: dict = {"repo": {"path": str(repo), "version": ""}, "copies": {}, "drift": False,
                    "unknown": False, "expected": "", "runtime_deps": {}}

    package_init = repo / "aletheia" / "__init__.py"
    report["repo"]["version"] = _read_version(package_init)
    expected = report["repo"]["version"]
    report["expected"] = expected

    report["runtime_deps"] = runtime_deps_report(repo, hermes_home, environment_override)
    # Only the copies that could be what the gateway runs are counted as drift: the resolved
    # environment, the profile's plugin directory, and the checkout. A retired environment left on
    # disk at an older version is worth printing and not worth failing the check — a check that
    # always fails is a check nobody reads. Resolved here, from the install facts, rather than from
    # the dependency checker: version drift must be answerable even when that module cannot load.
    active_id = _active_environment_id(hermes_home, environment_override)

    def note(label: str, version: str, path: Path, *, judged: bool) -> None:
        report["copies"][label] = {"version": version or "unknown", "path": str(path), "judged": judged}

    note("repo_package", expected, package_init, judged=True)
    plugin_dir = hermes_home / "plugins" / "aletheia"
    note("plugins_dir", _plugin_yaml_version(plugin_dir / "plugin.yaml"), plugin_dir, judged=True)

    for install_env in sorted(hermes_home.glob("installs/*/environments/*")):
        judged = install_env.name[:8] == active_id
        for source in sorted(install_env.glob("workspace/plugin-sources/aletheia-*")):
            note(f"plugin_sources:{install_env.name[:8]}",
                 _plugin_yaml_version(source / "plugin.yaml"), source, judged=judged)
        for site_packages in sorted(install_env.glob("venv/lib/python*/site-packages")):
            # An editable install is a shim: its dist-info carries the version it was installed at,
            # while the code comes from the source tree its finder maps to — which the plugin_sources
            # entry already judges. Counting the shim's metadata as drift reports a stale number
            # rather than different code.
            editable = bool(list(site_packages.glob("__editable__*aletheia*.pth"))
                            or list(site_packages.glob("__editable__*ousia*.pth")))
            for dist, version in _dist_versions(site_packages).items():
                note(f"dist:{install_env.name[:8]}", version, Path(dist), judged=judged and not editable)

    # A matching version that cannot import its embedder is not a working deployment: the gateway
    # starts, reports the provider unavailable, and external memory is silently off.
    report["drift"] = any(copy["version"] != expected for copy in report["copies"].values()
                          if copy["version"] != "unknown" and copy.get("judged"))
    deps_verdict = report["runtime_deps"].get("verdict")
    if deps_verdict == "drift":
        report["drift"] = True
    elif deps_verdict == "unknown":
        report["unknown"] = True
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", default=str(Path(__file__).resolve().parent.parent))
    parser.add_argument("--hermes-home", default=os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
    parser.add_argument("--env", default=None, help="an environment's venv directory to judge")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    override = Path(args.env).expanduser() if args.env else None
    report = collect(Path(args.repo), Path(args.hermes_home), override)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"repository: {report['expected'] or 'unknown'}  ({report['repo']['path']})")
        for label, copy in sorted(report["copies"].items()):
            if copy["version"] == "unknown":
                mark = "?"  # a path with no readable version is unknown, not a disagreement
            elif not copy.get("judged"):
                mark = "-"  # a retired environment: printed, not counted
            else:
                mark = "ok" if copy["version"] == report["expected"] else "DRIFT"
            print(f"  {mark:<5} {label:<28} {copy['version']:<10} {copy['path']}")
        deps = report["runtime_deps"]
        verdict = deps.get("verdict", "unknown")
        if verdict == "drift":
            if deps.get("missing"):
                print(f"  DRIFT runtime_deps            missing: {', '.join(deps['missing'])}")
            if deps.get("declaration") == "incomplete":
                print(f"  DRIFT declaration             not installed by [project.dependencies] "
                      f"on {deps.get('target', '?')}: {', '.join(deps.get('undeclared') or [])}")
            print(f"        environment: {deps.get('environment', '')}")
            for module, reason in (deps.get("import_errors") or {}).items():
                print(f"        {module}: {reason}")
            if deps.get("repair"):
                print(f"        repair: {deps['repair']}")
        elif verdict == "unknown":
            print(f"  ?     runtime_deps            unknown: {deps.get('error', '')}")
        else:
            print(f"  ok    runtime_deps             importable in {deps.get('environment', '')}"
                  f" ({deps.get('target', '?')})")
        if deps.get("note"):
            print(f"        note: {deps['note']}")
        if report["drift"]:
            print("drift detected")
        elif report["unknown"]:
            print("unknown: the deployment could not be fully judged — not a pass")
        else:
            print("no drift")
    if report["drift"]:
        return 1
    return 2 if report["unknown"] else 0


if __name__ == "__main__":
    sys.exit(main())