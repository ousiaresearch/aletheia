"""The deploy check must see every copy, and must fail when one of them is a different version."""

from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "verify_deployed.py"


def _load():
    spec = importlib.util.spec_from_file_location("verify_deployed", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_reports_no_drift_when_every_copy_matches(tmp_path):
    module = _load()
    repo = tmp_path / "repo"
    home = tmp_path / ".hermes"
    _write(repo / "aletheia" / "__init__.py", '__version__ = "2.1.0"\n')
    _write(home / "plugins" / "aletheia" / "plugin.yaml", "name: aletheia\nversion: 2.1.0\n")
    _write(home / "installs" / "i" / "environments" / "env123456"
           / "workspace" / "plugin-sources" / "aletheia-abc" / "plugin.yaml",
           "name: aletheia\nversion: 2.1.0\n")

    report = module.collect(repo, home)
    assert report["expected"] == "2.1.0"
    assert report["drift"] is False


def test_detects_a_stale_plugin_source_snapshot(tmp_path):
    module = _load()
    repo = tmp_path / "repo"
    home = tmp_path / ".hermes"
    _write(repo / "aletheia" / "__init__.py", '__version__ = "2.1.0"\n')
    _write(home / "plugins" / "aletheia" / "plugin.yaml", "name: aletheia\nversion: 2.1.0\n")
    # This is the shape that hid in production: the gateway imported a 2.0.0 snapshot while the
    # repository said 2.0.1.
    _write(home / "installs" / "i" / "environments" / "env123456"
           / "workspace" / "plugin-sources" / "aletheia-abc" / "plugin.yaml",
           "name: aletheia\nversion: 2.0.0\n")
    # Only the environment facts.json resolves to is judged; a copy found in a retired environment
    # is printed for information. This is the shape the gateway was in.
    (home / "installs" / "i" / "environments" / "env123456" / "venv").mkdir(parents=True)
    _write(home / "installs" / "i" / "facts.json",
           '{"packages": {"venv": {"environment": "%s"}}}\n'
           % (home / "installs" / "i" / "environments" / "env123456" / "venv"))

    report = module.collect(repo, home)
    assert report["drift"] is True
    stale = [c for c in report["copies"].values() if c["version"] == "2.0.0"]
    assert stale, "the stale copy must be named, not just counted"


def test_detects_a_stale_distribution(tmp_path):
    module = _load()
    repo = tmp_path / "repo"
    home = tmp_path / ".hermes"
    _write(repo / "aletheia" / "__init__.py", '__version__ = "2.1.0"\n')
    env = home / "installs" / "i" / "environments" / "env123456" / "venv"
    dist = env / "lib" / "python3.14" / "site-packages" / "ousia_aletheia-2.0.0.dist-info"
    _write(dist / "METADATA", "Name: ousia-aletheia\nVersion: 2.0.0\n")
    (env / "bin").mkdir(parents=True, exist_ok=True)
    _write(home / "installs" / "i" / "facts.json",
           '{"packages": {"venv": {"environment": "%s"}}}\n' % env)

    report = module.collect(repo, home)
    assert report["drift"] is True
    dists = [c for label, c in report["copies"].items() if label.startswith("dist:")]
    assert dists and all(c["judged"] for c in dists), "a wheel install in the resolved environment is judged"


def test_exit_code_follows_drift(tmp_path, capsys):
    module = _load()
    repo = tmp_path / "repo"
    home = tmp_path / ".hermes"
    _write(repo / "aletheia" / "__init__.py", '__version__ = "2.1.0"\n')
    _write(home / "plugins" / "aletheia" / "plugin.yaml", "name: aletheia\nversion: 2.0.0\n")
    assert module.main(["--repo", str(repo), "--hermes-home", str(home), "--json"]) == 1
    assert "drift" in capsys.readouterr().out