import importlib.util
import tomllib
from pathlib import Path


def test_local_build_metadata_declares_version_and_entrypoint():
    repo = Path(__file__).resolve().parents[1]
    metadata = tomllib.loads((repo / 'pyproject.toml').read_text())
    project = metadata['project']
    assert project['name'] == 'ousia-aletheia'
    assert project['version'] == '2.0.1'
    # The version is validated as a PEP 440 string and kept in sync with the plugin.
    import re
    assert re.fullmatch(r'\d+(\.\d+)+(a\d+|b\d+|rc\d+)?', project['version']), project['version']
    module_path = repo / 'aletheia' / '__init__.py'
    spec = importlib.util.spec_from_file_location('_aletheia_package_version', module_path)
    assert spec is not None and spec.loader is not None
    source_package = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(source_package)
    assert source_package.__version__ == project['version']
    manifest_version = next(line.split(':', 1)[1].strip() for line in (repo / 'plugin.yaml').read_text().splitlines() if line.startswith('version: '))
    assert manifest_version == project['version']
    assert any(line.startswith(f'## [{project["version"]}]') for line in (repo / 'CHANGELOG.md').read_text().splitlines())
    assert project['requires-python'] == '>=3.11'
    assert project['scripts']['aletheia'] == 'aletheia.cli:entrypoint'
    assert any(dep.startswith('sqlite-vec') for dep in project['dependencies'])
    assert any(dep.startswith('pypdf') for dep in project['dependencies'])
    assert any(dep.startswith('python-docx') for dep in project['dependencies'])
    assert any(dep.startswith('mlx-embeddings') for dep in project['optional-dependencies']['local-embedding'])
