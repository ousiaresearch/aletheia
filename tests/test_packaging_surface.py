"""Packaging surface: what the wheel actually contains.

A built wheel is the artifact strangers install, so the test that matters is about the wheel's inputs:
every package directory in the tree must be declared, because `packages = ["weft"]` silently dropped
`weft/providers/` from the first a1 wheel while `./scripts/build-release.sh` still reported PASSED —
it checked metadata, a clean-venv install and the entrypoint, none of which touch the new subpackage.
"""

from __future__ import annotations

import pathlib
import tomllib

REPO = pathlib.Path(__file__).resolve().parents[1]


def _declared_packages() -> set[str]:
    config = tomllib.loads((REPO / "pyproject.toml").read_text())
    return set(config["tool"]["setuptools"]["packages"])


def test_every_package_directory_is_declared():
    declared = _declared_packages()
    missed = []
    for directory in sorted(REPO.joinpath("weft").rglob("*")):
        if directory.is_dir() and (directory / "__init__.py").is_file():
            name = directory.relative_to(REPO).as_posix().replace("/", ".")
            if name not in declared:
                missed.append(name)
    assert missed == [], f"package directories missing from [tool.setuptools] packages: {missed}"


def test_the_hermes_provider_is_declared_and_present_on_disk():
    """The provider is the one module a catalog install depends on: it must ship."""
    assert "weft.providers" in _declared_packages()
    assert (REPO / "weft" / "providers" / "hermes.py").is_file()


def test_the_plugin_manifest_sits_beside_its_entry_point():
    manifest = REPO / "hermes-plugin" / "plugin.yaml"
    assert manifest.is_file() and (manifest.parent / "__init__.py").is_file()
    text = manifest.read_text()
    assert "weft-memory>=" in text, "the manifest must name the package it needs"
