#!/usr/bin/env python3
"""Report which Aletheia version is actually deployed, and fail when the copies disagree.

A plugin can exist in four places at once: the development checkout, the profile's ``plugins/``
directory, the plugin host's copied source tree, and an installed distribution in one of the
gateway environments. The gateway imports one of them. Reading the repository proves nothing about
the running process, so this script reports every copy it can find and exits non-zero when their
versions differ from the repository's.

Usage:
    python scripts/verify_deployed.py [--repo PATH] [--hermes-home PATH] [--json]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

VERSION_RE = re.compile(r"""__version__\s*=\s*["']([^"']+)["']""")


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


def _dist_versions(site_packages: Path) -> dict[str, str]:
    found: dict[str, str] = {}
    for entry in sorted(site_packages.glob("aletheia-*.dist-info")):
        metadata = entry / "METADATA"
        try:
            for line in metadata.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("Version:"):
                    found[str(entry)] = line.split(":", 1)[1].strip()
                    break
        except OSError:
            continue
    return found


def collect(repo: Path, hermes_home: Path) -> dict:
    repo = Path(repo).expanduser()
    hermes_home = Path(hermes_home).expanduser()
    report: dict = {"repo": {"path": str(repo), "version": ""}, "copies": {}, "drift": False,
                    "expected": ""}

    package_init = repo / "aletheia" / "__init__.py"
    report["repo"]["version"] = _read_version(package_init)
    expected = report["repo"]["version"]
    report["expected"] = expected

    def note(label: str, version: str, path: Path) -> None:
        report["copies"][label] = {"version": version or "unknown", "path": str(path)}

    note("repo_package", expected, package_init)
    plugin_dir = hermes_home / "plugins" / "aletheia"
    note("plugins_dir", _plugin_yaml_version(plugin_dir / "plugin.yaml"), plugin_dir)

    for install_env in sorted(hermes_home.glob("installs/*/environments/*")):
        for source in sorted(install_env.glob("workspace/plugin-sources/aletheia-*")):
            note(f"plugin_sources:{install_env.name[:8]}",
                 _plugin_yaml_version(source / "plugin.yaml"), source)
        for site_packages in sorted(install_env.glob("venv/lib/python*/site-packages")):
            for dist, version in _dist_versions(site_packages).items():
                note(f"dist:{install_env.name[:8]}", version, Path(dist))

    report["drift"] = any(copy["version"] != expected for copy in report["copies"].values()
                          if copy["version"] != "unknown")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=str(Path(__file__).resolve().parent.parent))
    parser.add_argument("--hermes-home", default=os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    report = collect(Path(args.repo), Path(args.hermes_home))
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"repository: {report['expected'] or 'unknown'}  ({report['repo']['path']})")
        for label, copy in sorted(report["copies"].items()):
            mark = "ok" if copy["version"] == report["expected"] else "DRIFT"
            print(f"  {mark:<5} {label:<28} {copy['version']:<10} {copy['path']}")
        print("drift detected" if report["drift"] else "no drift")
    return 1 if report["drift"] else 0


if __name__ == "__main__":
    sys.exit(main())