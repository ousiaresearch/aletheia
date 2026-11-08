import tomllib
from pathlib import Path


def test_local_build_metadata_declares_version_and_entrypoint():
    repo = Path(__file__).resolve().parents[1]
    metadata = tomllib.loads((repo / 'pyproject.toml').read_text())
    project = metadata['project']
    assert project['name'] == 'aletheia'
    # The version is a PEP 440 string; it carried a pre-release suffix while the surface moved.
    import re
    assert re.fullmatch(r'\d+(\.\d+)+(a\d+|b\d+|rc\d+)?', project['version']), project['version']
    import aletheia
    assert aletheia.__version__ == project['version']
    assert project['requires-python'] == '>=3.11'
    assert project['scripts']['aletheia'] == 'aletheia.cli:entrypoint'
    assert any(dep.startswith('sqlite-vec') for dep in project['dependencies'])
    assert any(dep.startswith('pypdf') for dep in project['dependencies'])
    assert any(dep.startswith('python-docx') for dep in project['dependencies'])
    assert any(dep.startswith('mlx-embeddings') for dep in project['optional-dependencies']['local-embedding'])
