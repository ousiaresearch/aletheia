"""Keep local reranking dependency declarations compatible with local_files_only."""
from pathlib import Path
import tomllib

from packaging.requirements import Requirement
from packaging.version import Version


ROOT = Path(__file__).resolve().parents[1]


def test_local_recall_dependency_requires_cross_encoder_local_files_only_support():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    requirements = [Requirement(value) for value in config["project"]["optional-dependencies"]["local-recall"]]
    requirement = next(req for req in requirements if req.name == "sentence-transformers")
    assert Version("2.7.0") not in requirement.specifier
    assert Version("3.0.0") in requirement.specifier


def test_bundled_plugin_requires_cross_encoder_local_files_only_support():
    manifest = (ROOT / "aletheia" / "plugin.yaml").read_text()
    requirement = next(
        Requirement(line.strip()[3:-1])
        for line in manifest.splitlines()
        if line.strip().startswith('- "sentence-transformers')
    )
    assert Version("2.7.0") not in requirement.specifier
    assert Version("3.0.0") in requirement.specifier
