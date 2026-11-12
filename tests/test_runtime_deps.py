"""The provider's requirements must be declared where Hermes installs from, and the check must lie
in neither direction: no "present" for a module that cannot be imported, and no "no drift" for an
environment it could not look at.

The failure these tests exist for: an environment was rebuilt, its declaration was read from
``pyproject.toml`` (which named only pypdf, python-docx and sqlite-vec), the embedder and the
reranker were left out because they lived in an extra, and the gateway came up reporting the memory
provider unavailable. External memory was off for every session and nothing failed.

Two rounds of adversarial review found the first version of this suite decorative: it monkeypatched
away the code that carries the risk, its exit-code test recomputed the answer with the call it was
testing, and a ``win32``-marked requirement passed as declared. Those cases are tests now.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shlex
import sys
from pathlib import Path

from aletheia import runtime_deps

ROOT = Path(__file__).resolve().parent.parent
VERIFY = ROOT / "scripts" / "verify_deployed.py"
CHECK = ROOT / "scripts" / "check_runtime_deps.py"
DARWIN_ARM = runtime_deps.Target(system="darwin", machine="arm64")
LINUX = runtime_deps.Target(system="linux", machine="x86_64")
SENTINEL = "__ALETHEIA_PROBE__"
READY = "__ALETHEIA_READY__"


# --------------------------------------------------------------------------- fixtures and helpers


def _fake_interpreter(path: Path, lines: list[str], *, raw_bytes: bytes = b"") -> Path:
    """A stand-in interpreter that prints what the probe parser must cope with."""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "#!/bin/sh\n"
    for line in lines:
        body += f"printf '%s\\n' {shlex.quote(line)}\n"
    if raw_bytes:
        body += f"printf '{raw_bytes.decode('latin-1')}' \n"
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _probe_payload(present: dict[str, bool], *, system: str = "darwin", machine: str = "arm64",
                   errors: dict[str, str] | None = None) -> str:
    modules = {name: {"present": ok, "error": (errors or {}).get(name, "")} for name, ok in present.items()}
    return SENTINEL + json.dumps({"modules": modules, "platform": {"system": system, "machine": machine}})


def _environment(tmp_path: Path, interpreter: Path | str) -> Path:
    """An environment shaped the way the callers expect: <env>/venv/bin/python."""
    venv = tmp_path / "env" / "venv"
    (venv / "bin").mkdir(parents=True, exist_ok=True)
    interpreter = Path(interpreter)
    target = venv / "bin" / "python"
    if interpreter.is_symlink() or interpreter.exists():
        try:
            os.link(interpreter, target)
        except OSError:
            target.write_bytes(interpreter.read_bytes())
            target.chmod(0o755)
    return venv


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _pyproject_with(tmp_path: Path, dependencies: list[str]) -> Path:
    body = "[project]\nname = 'x'\nversion = '0'\ndependencies = [\n"
    body += "".join(f"  {json.dumps(dep)},\n" for dep in dependencies)
    body += "]\n"
    path = tmp_path / "pyproject.toml"
    path.write_text(body, encoding="utf-8")
    return path


def _plugin_yaml_specs() -> list[str]:
    """The flat string entries of plugin.yaml, quotes removed only when they are a pair."""
    specs = []
    for line in (ROOT / "plugin.yaml").read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped.startswith("- "):
            continue
        entry = stripped[2:].strip()
        if len(entry) >= 2 and entry[0] == entry[-1] and entry[0] in "\"'":
            entry = entry[1:-1]
        specs.append(entry)
    return specs


# --------------------------------------------------------------------------- declaration


def test_core_dependencies_declare_every_runtime_requirement():
    assert runtime_deps.missing_declarations() == [], (
        "an environment rebuilt from [project.dependencies] would not install the provider's "
        "runtime requirements; they cannot live only in an extra"
    )


def test_a_requirement_marked_for_another_platform_counts_as_undeclared(tmp_path):
    """The first version compared names only, so a win32 marker passed and installed nowhere."""
    declaration = _pyproject_with(tmp_path, [
        "sentence-transformers>=3.0,<6; sys_platform == 'win32'",
        "mlx>=0.32.4,<0.33; sys_platform == 'darwin' and platform_machine == 'arm64'",
        "mlx-embeddings==0.1.0; sys_platform == 'darwin' and platform_machine == 'arm64'",
    ])
    assert runtime_deps.missing_declarations(declaration, DARWIN_ARM) == ["sentence-transformers"]
    # Nothing is required off Apple Silicon, so an empty set is the right answer there rather than
    # a demand for a wheel that cannot be built.
    assert runtime_deps.missing_declarations(declaration, runtime_deps.Target("win32", "amd64")) == []


def test_platform_markers_keep_the_requirements_off_other_platforms():
    assert runtime_deps.required_modules(LINUX) == {}
    assert runtime_deps.missing_declarations(target=LINUX) == []


def test_the_requirements_are_marked_for_apple_silicon(tmp_path):
    applicable = runtime_deps.applicable_declarations() or {}
    for distribution in ("mlx", "mlx-embeddings", "sentence-transformers"):
        spec = applicable[distribution]
        assert "sys_platform == 'darwin'" in spec and "platform_machine == 'arm64'" in spec
        assert runtime_deps.requirement_applies(spec, LINUX) is False


def test_no_declaration_is_unknown_rather_than_undeclared(tmp_path):
    """An installed copy has no pyproject; that must read as unknown, not as a defect."""
    missing = tmp_path / "nope" / "pyproject.toml"
    assert runtime_deps.declared_requirements(missing) is None
    assert runtime_deps.missing_declarations(missing) is None


def test_url_and_legacy_requirements_are_parsed_not_misread(tmp_path):
    """A URL-pinned dependency must not read as absent and send the operator to install a fork."""
    dependency = "sentence-transformers @ https://example.invalid/st-4.0.0-py3-none-any.whl"
    declaration = _pyproject_with(tmp_path, [
        dependency,
        "mlx>=0.32.4,<0.33; sys_platform == 'darwin' and platform_machine == 'arm64'",
        "mlx-embeddings==0.1.0; sys_platform == 'darwin' and platform_machine == 'arm64'",
    ])
    requirement = runtime_deps.parse_requirement(dependency)
    assert requirement is not None and runtime_deps._canonical(requirement.name) == "sentence-transformers"
    assert runtime_deps.missing_declarations(declaration, DARWIN_ARM) == []


def test_plugin_yaml_mirrors_the_requirements_whatever_the_formatting():
    """Text containment, not a hand-rolled YAML parser: flow style and inline comments both count."""
    yaml_specs = _plugin_yaml_specs()
    mirrored = 0
    for distribution, spec in (runtime_deps.applicable_declarations() or {}).items():
        if distribution not in runtime_deps.MODULE_DISTRIBUTIONS.values():
            continue  # packaging and the document libraries are supplied with the package
        assert spec in yaml_specs, f"plugin.yaml does not mirror {spec!r}; it has {yaml_specs}"
        mirrored += 1
    assert mirrored == len(runtime_deps.MODULE_DISTRIBUTIONS), "not every runtime pin was compared"
    assert any(spec.startswith("ousia-aletheia") for spec in yaml_specs)


def test_the_repair_command_is_executable_in_a_pip_less_environment(tmp_path, monkeypatch):
    """Hermes environments have uv and no pip: `python -m pip install` fails the moment it is pasted."""
    command = runtime_deps.install_command("/env/bin/python", ["sentence-transformers"], uv=tmp_path / "uv")
    assert "pip install --python /env/bin/python" in command
    assert "sentence-transformers>=3.0,<6" in command and "sys_platform" in command

    monkeypatch.setattr(runtime_deps, "find_uv", lambda: None)
    fallback = runtime_deps.install_command("/env/bin/python", ["mlx"], uv=None)
    assert fallback.startswith("/env/bin/python -m pip install")


# --------------------------------------------------------------------------- presence


def test_probe_separates_present_from_missing_modules():
    result = runtime_deps.probe(sys.executable, {"json": "json", "definitely_not_a_module": "nothing"})
    assert result.error == ""
    assert result.found["json"] is True
    assert result.missing == ["definitely_not_a_module"]
    assert result.target is not None and result.target.system


def test_probe_reads_an_import_error_rather_than_a_spec_lookup(tmp_path):
    """find_spec cannot see a broken __init__; the probe must."""
    interpreter = _fake_interpreter(
        tmp_path / "fake" / "python",
        [_probe_payload({"sentence_transformers": False}, errors={"sentence_transformers": "ImportError: libomp"})],
    )
    result = runtime_deps.probe(interpreter, {"sentence_transformers": "sentence-transformers"})
    assert result.missing == ["sentence_transformers"]
    assert "libomp" in result.errors["sentence_transformers"]


def test_probe_ignores_banner_noise_before_its_result(tmp_path):
    interpreter = _fake_interpreter(tmp_path / "fake" / "python", [
        "sitecustomize: something harmless",
        _probe_payload({"mlx": True}),
    ])
    result = runtime_deps.probe(interpreter, {"mlx": "mlx"})
    assert result.error == "" and result.found == {"mlx": True}


def test_probe_survives_output_that_is_not_utf8(tmp_path):
    interpreter = tmp_path / "fake" / "python"
    interpreter.parent.mkdir(parents=True, exist_ok=True)
    interpreter.write_bytes(
        b"#!/bin/sh\nprintf '\\377\\376 not utf8\\n'\n"
        + f"printf '%s\\n' {shlex.quote(_probe_payload({'mlx': True}))}\n".encode()
    )
    interpreter.chmod(0o755)
    result = runtime_deps.probe(interpreter, {"mlx": "mlx"})
    assert result.error == "", "a non-UTF8 banner must not raise out of the probe"
    assert result.found == {"mlx": True}


def test_probe_reports_a_missing_interpreter_instead_of_raising(tmp_path):
    result = runtime_deps.probe(tmp_path / "venv" / "bin" / "python", {"json": "json"})
    assert result.found == {}
    assert "no interpreter" in result.error


def test_probe_cannot_be_answered_by_the_callers_working_directory(tmp_path):
    """A `sentence_transformers/` folder in cwd must never answer for the install."""
    shadow = tmp_path / "shadow"
    (shadow / "sentence_transformers").mkdir(parents=True)
    (shadow / "sentence_transformers" / "__init__.py").write_text(
        "raise ImportError('shadow-marker')\n", encoding="utf-8")
    result = runtime_deps.probe(sys.executable, {"sentence_transformers": "sentence-transformers"},
                                cwd=shadow)
    assert "shadow-marker" not in (result.errors.get("sentence_transformers") or "")


# --------------------------------------------------------------------------- environment


def _facts(tmp_path: Path, venv: Path, *, lock_env: str | None = None, install: str = "i") -> Path:
    facts = tmp_path / "installs" / install / "facts.json"
    facts.parent.mkdir(parents=True, exist_ok=True)
    document = {"packages": {"venv": {"environment": str(venv)}}}
    if lock_env:
        document["packages"]["venv"]["resolved_lock"] = str(
            tmp_path / "installs" / install / "environments" / lock_env / "workspace" / "uv.lock")
    facts.write_text(json.dumps(document), encoding="utf-8")
    return facts


def test_active_environment_reads_install_facts(tmp_path):
    venv = _environment(tmp_path, sys.executable)
    _facts(tmp_path, venv)
    assert runtime_deps.active_environment(tmp_path).venv == venv


def test_active_environment_prefers_the_newest_facts(tmp_path):
    environments = {}
    for name, stamp in (("old", 1_000_000), ("new", 2_000_000)):
        venv = _environment(tmp_path / name, sys.executable)
        facts = _facts(tmp_path, venv, install=name)
        os.utime(facts, (stamp, stamp))
        environments[name] = venv
    resolved = runtime_deps.active_environment(tmp_path)
    assert resolved.venv == environments["new"]


def test_active_environment_is_unknown_without_facts(tmp_path):
    resolved = runtime_deps.active_environment(tmp_path)
    assert resolved.venv is None and resolved.note == ""


def test_a_facts_entry_pointing_nowhere_is_not_an_environment(tmp_path):
    _facts(tmp_path, tmp_path / "gone")
    assert runtime_deps.active_environment(tmp_path).venv is None


def test_a_disagreement_between_named_environment_and_lock_is_reported(tmp_path):
    """This machine named one venv while resolving the lock in another; only one had the models."""
    venv = _environment(tmp_path, sys.executable)
    _facts(tmp_path, venv, lock_env="e2feeeac5af7")
    note = runtime_deps.active_environment(tmp_path).note
    assert "resolves the lock in e2feeeac" in note and "used here" in note


# --------------------------------------------------------------------------- the checker CLI


def test_check_script_reports_an_unknown_environment_as_exit_2(tmp_path, capsys):
    module = _load(CHECK, "check_runtime_deps_unknown")
    code = module.main(["--hermes-home", str(tmp_path), "--json"])
    assert code == 2
    assert "no resolved environment" in capsys.readouterr().out


def test_check_script_exits_1_and_names_the_repair_for_a_missing_requirement(tmp_path, capsys):
    fake = _fake_interpreter(tmp_path / "fake" / "python",
                             [_probe_payload({"mlx": True, "mlx_embeddings": True,
                                              "sentence_transformers": False})])
    venv = _environment(tmp_path, fake)
    code = _load(CHECK, "check_runtime_deps_missing").main(
        ["--env", str(venv), "--repo", str(ROOT), "--hermes-home", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 1
    assert "MISSING  sentence_transformers" in out
    assert "pip install --python" in out, "the repair must be a command that works without pip"


def test_check_script_exits_0_when_every_module_imports(tmp_path, capsys):
    fake = _fake_interpreter(tmp_path / "fake" / "python",
                             [_probe_payload({"mlx": True, "mlx_embeddings": True,
                                              "sentence_transformers": True})])
    venv = _environment(tmp_path, fake)
    code = _load(CHECK, "check_runtime_deps_ok").main(
        ["--env", str(venv), "--repo", str(ROOT), "--hermes-home", str(tmp_path)])
    assert code == 0
    assert "all requirements present" in capsys.readouterr().out


def test_check_script_does_not_leak_the_repository_onto_sys_path(tmp_path):
    before = list(sys.path)
    _load(CHECK, "check_runtime_deps_path").main(["--hermes-home", str(tmp_path), "--json"])
    assert sys.path == before


def test_check_script_treats_an_unreadable_probe_as_unknown(tmp_path, capsys):
    fake = _fake_interpreter(tmp_path / "fake" / "python", ["definitely not json"])
    venv = _environment(tmp_path, fake)
    code = _load(CHECK, "check_runtime_deps_unprobeable").main(
        ["--env", str(venv), "--repo", str(ROOT), "--hermes-home", str(tmp_path)])
    assert code == 2
    assert "unknown" in capsys.readouterr().out.lower()


# --------------------------------------------------------------------------- verify_deployed


def _repo_copy(tmp_path: Path, version: str = "2.1.0", *, with_runtime_deps: bool = True) -> Path:
    repo = tmp_path / "repo"
    (repo / "aletheia").mkdir(parents=True, exist_ok=True)
    (repo / "aletheia" / "__init__.py").write_text(f'__version__ = "{version}"\n', encoding="utf-8")
    if with_runtime_deps:
        (repo / "aletheia" / "runtime_deps.py").write_text(
            (ROOT / "aletheia" / "runtime_deps.py").read_text(encoding="utf-8"), encoding="utf-8")
    return repo


def _deployed(repo: Path, hermes_home: Path) -> None:
    (hermes_home / "plugins" / "aletheia").mkdir(parents=True, exist_ok=True)
    (hermes_home / "plugins" / "aletheia" / "plugin.yaml").write_text(
        'name: aletheia\nversion: "2.1.0"\n', encoding="utf-8")


def test_verify_deployed_uses_the_repository_it_was_given(tmp_path):
    """Two checkouts in one process: the second must not be answered by the first.

    The first version imported ``aletheia.runtime_deps`` through whatever package this interpreter
    resolved, so a report about repo B could be computed from repo A's declaration.
    """
    module = _load(VERIFY, "verify_deployed_repos")
    good = _repo_copy(tmp_path / "a")
    bad = _repo_copy(tmp_path / "b")
    (good / "pyproject.toml").write_text(
        (ROOT / "pyproject.toml").read_text(encoding="utf-8"), encoding="utf-8")
    (bad / "pyproject.toml").write_text(
        "[project]\nname = 'x'\nversion = '0'\n"
        "dependencies = [\"sentence-transformers>=3.0,<6; sys_platform == 'win32'\", "
        "\"mlx>=0.32.4,<0.33; sys_platform == 'darwin' and platform_machine == 'arm64'\", "
        "\"mlx-embeddings==0.1.0; sys_platform == 'darwin' and platform_machine == 'arm64'\"]\n",
        encoding="utf-8")
    fake = _fake_interpreter(tmp_path / "fake" / "python",
                             [_probe_payload({"mlx": True, "mlx_embeddings": True,
                                              "sentence_transformers": True})])
    venv = _environment(tmp_path, fake)
    for name, repo in (("home_a", good), ("home_b", bad)):
        _deployed(repo, tmp_path / name)
        _facts(tmp_path / name, venv)

    good_report = module.collect(good, tmp_path / "home_a")
    bad_report = module.collect(bad, tmp_path / "home_b")
    assert good_report["runtime_deps"]["declaration"] == "ok"
    assert bad_report["runtime_deps"]["declaration"] == "incomplete"
    assert bad_report["runtime_deps"]["undeclared"] == ["sentence-transformers"]


def test_verify_deployed_is_unknown_not_clean_when_the_checker_cannot_load(tmp_path):
    module = _load(VERIFY, "verify_deployed_nochecker")
    repo = _repo_copy(tmp_path / "repo", with_runtime_deps=False)
    _deployed(repo, tmp_path / "home")
    report = module.collect(repo, tmp_path / "home")
    assert report["drift"] is False
    assert report["unknown"] is True
    assert module.main(["--repo", str(repo), "--hermes-home", str(tmp_path / "home"), "--json"]) == 2


def test_verify_deployed_is_unknown_not_clean_when_the_environment_is_not_resolved(tmp_path):
    module = _load(VERIFY, "verify_deployed_noenv")
    repo = _repo_copy(tmp_path / "repo")
    _deployed(repo, tmp_path / "home")
    report = module.collect(repo, tmp_path / "home")
    assert report["runtime_deps"]["verdict"] == "unknown"
    assert report["unknown"] is True and report["drift"] is False
    assert module.main(["--repo", str(repo), "--hermes-home", str(tmp_path / "home")]) == 2


def test_verify_deployed_calls_a_missing_requirement_drift(tmp_path):
    module = _load(VERIFY, "verify_deployed_missing")
    repo = _repo_copy(tmp_path / "repo")
    (repo / "pyproject.toml").write_text(
        (ROOT / "pyproject.toml").read_text(encoding="utf-8"), encoding="utf-8")
    home = tmp_path / "home"
    _deployed(repo, home)
    fake = _fake_interpreter(tmp_path / "fake" / "python",
                             [_probe_payload({"mlx": False, "mlx_embeddings": False,
                                              "sentence_transformers": False})])
    venv = _environment(tmp_path, fake)
    _facts(home, venv)

    report = module.collect(repo, home)
    assert report["runtime_deps"]["verdict"] == "drift"
    assert "sentence-transformers" in report["runtime_deps"]["missing"]
    assert report["drift"] is True
    assert module.main(["--repo", str(repo), "--hermes-home", str(home)]) == 1


def test_verify_deployed_calls_an_undeclared_requirement_drift(tmp_path):
    module = _load(VERIFY, "verify_deployed_undeclared")
    repo = _repo_copy(tmp_path / "repo")
    (repo / "pyproject.toml").write_text(
        "[project]\nname = 'x'\nversion = '0'\ndependencies = []\n", encoding="utf-8")
    home = tmp_path / "home"
    _deployed(repo, home)
    fake = _fake_interpreter(tmp_path / "fake" / "python",
                             [_probe_payload({"mlx": True, "mlx_embeddings": True,
                                              "sentence_transformers": True})])
    _facts(home, _environment(tmp_path, fake))

    report = module.collect(repo, home)
    assert report["runtime_deps"]["declaration"] == "incomplete"
    assert report["drift"] is True, "present here is not declared: the next rebuild starts over"


def test_verify_deployed_is_clean_when_the_environment_can_import(tmp_path):
    module = _load(VERIFY, "verify_deployed_clean")
    repo = _repo_copy(tmp_path / "repo")
    (repo / "pyproject.toml").write_text(
        (ROOT / "pyproject.toml").read_text(encoding="utf-8"), encoding="utf-8")
    home = tmp_path / "home"
    _deployed(repo, home)
    fake = _fake_interpreter(tmp_path / "fake" / "python",
                             [_probe_payload({"mlx": True, "mlx_embeddings": True,
                                              "sentence_transformers": True})])
    _facts(home, _environment(tmp_path, fake))

    report = module.collect(repo, home)
    assert report["runtime_deps"]["verdict"] == "ok"
    assert report["drift"] is False and report["unknown"] is False
    assert module.main(["--repo", str(repo), "--hermes-home", str(home)]) == 0


# --------------------------------------------------------------------------- what a green run must not hide


def test_probe_treats_an_unreported_module_as_missing(tmp_path):
    """A child that answers about fewer modules than it was asked about must not imply the rest."""
    fake = _fake_interpreter(tmp_path / "fake" / "python", [_probe_payload({"json": True})])
    result = runtime_deps.probe(fake, {"json": "json", "mlx": "mlx"})
    assert result.error == ""
    assert result.found == {"json": True, "mlx": False}
    assert result.errors["mlx"] == "probe did not report this module"


def test_probe_rejects_a_namespace_directory_as_an_install(tmp_path):
    """An empty `mlx/` directory on the path imports, and is not an install.

    Built as a real venv because the detection lives in the child: a fabricated payload would prove
    only that the parser reads what it is handed.
    """
    venv = tmp_path / "nsvenv"
    import subprocess

    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True,
                   capture_output=True, timeout=180)
    site_packages = next(venv.glob("lib/python*/site-packages"))
    (site_packages / "mlx").mkdir()
    (site_packages / "broken_pkg").mkdir()
    (site_packages / "broken_pkg" / "__init__.py").write_text("raise ImportError('half-written')\n",
                                                              encoding="utf-8")

    result = runtime_deps.probe(venv / "bin" / "python", {"mlx": "mlx", "broken_pkg": "broken"})
    assert result.error == ""
    assert result.found["mlx"] is False, "an empty mlx/ directory is not an installed distribution"
    assert "namespace" in result.errors["mlx"]
    assert result.found["broken_pkg"] is False
    assert "half-written" in result.errors["broken_pkg"]


def test_probe_accepts_a_namespace_package_that_is_really_installed(tmp_path):
    """`mlx` ships as a namespace directory of extension modules: rejecting it would be a false alarm.

    The canary submodule is what separates "installed as a namespace" from "an empty folder on the
    path", and this is the case the first version of the namespace check got wrong against the live
    environment.
    """
    import subprocess

    venv = tmp_path / "nsvenv2"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True,
                   capture_output=True, timeout=180)
    site_packages = next(venv.glob("lib/python*/site-packages"))
    (site_packages / "mlx").mkdir()
    (site_packages / "mlx" / "core.py").write_text("VALUE = 1\n", encoding="utf-8")

    result = runtime_deps.probe(venv / "bin" / "python", {"mlx": "mlx"})
    assert result.found["mlx"] is True, result.errors.get("mlx")


def test_probe_ignores_a_module_in_the_process_working_directory(tmp_path, monkeypatch):
    """The production default cwd must be a scratch directory, not the caller's."""
    shadow = tmp_path / "cwd-shadow"
    (shadow / "sentence_transformers").mkdir(parents=True)
    (shadow / "sentence_transformers" / "__init__.py").write_text(
        "raise ImportError('cwd-shadow-marker')\n", encoding="utf-8")
    monkeypatch.chdir(shadow)

    result = runtime_deps.probe(sys.executable, {"sentence_transformers": "sentence-transformers"})
    assert "cwd-shadow-marker" not in (result.errors.get("sentence_transformers") or "")


def test_a_platform_marker_that_cannot_be_judged_is_unknown(tmp_path):
    """`os_name` and friends cannot be answered for a target that is not this process."""
    spec = "sentence-transformers>=3.0,<6; os_name == 'posix'"
    assert runtime_deps.requirement_applies(spec, LINUX) is None
    # The same marker on this process is answerable at all.
    assert runtime_deps.requirement_applies(spec, runtime_deps.local_target()) is not None


def test_python_version_markers_are_judged_against_the_target():
    spec = "sentence-transformers>=3.0,<6; python_version < '3.12'"
    assert runtime_deps.requirement_applies(spec, runtime_deps.Target("darwin", "arm64", "3.11")) is True
    assert runtime_deps.requirement_applies(spec, runtime_deps.Target("darwin", "arm64", "3.13")) is False


def test_the_repair_command_quotes_specs_safely(tmp_path):
    """A spec containing both quote kinds used to produce an unparseable command."""
    import shlex

    command = runtime_deps.install_command("/env/bin/python", ["sentence-transformers"], uv=tmp_path / "uv")
    shlex.split(command)  # raises ValueError if the quoting is broken
    assert shlex.split(command)[-1].startswith("sentence-transformers")


def test_check_script_is_not_green_on_a_platform_the_provider_cannot_run(tmp_path, capsys):
    """An empty requirement set used to read as 'all requirements present' with exit 0."""
    fake = _fake_interpreter(tmp_path / "fake" / "python",
                             [_probe_payload({"mlx": True, "mlx_embeddings": True,
                                              "sentence_transformers": True},
                                             system="linux", machine="x86_64")])
    venv = _environment(tmp_path, fake)
    code = _load(CHECK, "check_runtime_deps_foreign").main(
        ["--env", str(venv), "--repo", str(ROOT), "--hermes-home", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 2, "nothing was checked, so the answer is unknown, not healthy"
    assert "NOT JUDGED" in out and "not a pass" in out


def test_check_script_is_unknown_when_the_declaration_cannot_be_read(tmp_path, capsys):
    fake = _fake_interpreter(tmp_path / "fake" / "python",
                             [_probe_payload({"mlx": True, "mlx_embeddings": True,
                                              "sentence_transformers": True})])
    venv = _environment(tmp_path, fake)
    repo = _repo_copy(tmp_path / "repo")  # no pyproject.toml
    code = _load(CHECK, "check_runtime_deps_nodecl").main(
        ["--env", str(venv), "--repo", str(repo), "--hermes-home", str(tmp_path)])
    assert code == 2
    assert "declaration could not be read" in capsys.readouterr().out


def test_verify_deployed_is_unknown_when_the_declaration_cannot_be_read(tmp_path):
    module = _load(VERIFY, "verify_deployed_nodecl")
    repo = _repo_copy(tmp_path / "repo")  # no pyproject.toml
    home = tmp_path / "home"
    _deployed(repo, home)
    fake = _fake_interpreter(tmp_path / "fake" / "python",
                             [_probe_payload({"mlx": True, "mlx_embeddings": True,
                                              "sentence_transformers": True})])
    _facts(home, _environment(tmp_path, fake))
    report = module.collect(repo, home)
    assert report["runtime_deps"]["verdict"] == "unknown"
    assert report["unknown"] is True and report["drift"] is False
    assert module.main(["--repo", str(repo), "--hermes-home", str(home)]) == 2


def test_dist_versions_sees_the_distribution_name_this_project_actually_ships(tmp_path):
    """The installed dist is ousia_aletheia-2.1.0.dist-info; a bare `aletheia-*` glob saw nothing."""
    module = _load(VERIFY, "verify_deployed_dist")
    site_packages = tmp_path / "site-packages"
    for name, version in (("ousia_aletheia-2.0.1.dist-info", "2.0.1"),
                          ("aletheia_memory-1.0.dist-info", "1.0"),
                          ("aletheia-0.9.dist-info", "0.9")):
        directory = site_packages / name
        directory.mkdir(parents=True)
        dist_name = name.rsplit("-", 2)[0]
        (directory / "METADATA").write_text(f"Name: {dist_name}\nVersion: {version}\n", encoding="utf-8")

    found = module._dist_versions(site_packages)
    assert sorted(found.values()) == ["0.9", "2.0.1"], "ousia-aletheia and aletheia are ours; aletheia_memory is not"


def test_verify_deployed_calls_a_stale_installed_distribution_drift(tmp_path):
    module = _load(VERIFY, "verify_deployed_staledist")
    repo = _repo_copy(tmp_path / "repo", version="2.1.0")
    (repo / "pyproject.toml").write_text(
        (ROOT / "pyproject.toml").read_text(encoding="utf-8"), encoding="utf-8")
    home = tmp_path / "home"
    _deployed(repo, home)
    fake = _fake_interpreter(tmp_path / "fake" / "python",
                             [_probe_payload({"mlx": True, "mlx_embeddings": True,
                                              "sentence_transformers": True})])
    # The environment lives where collect() looks for one, and is the one facts.json resolves.
    env = home / "installs" / "i" / "environments" / "abcdef1234567890" / "venv"
    (env / "bin").mkdir(parents=True)
    os.link(fake, env / "bin" / "python")
    dist = env / "lib" / "python3.12" / "site-packages" / "ousia_aletheia-2.0.1.dist-info"
    dist.mkdir(parents=True)
    (dist / "METADATA").write_text("Name: ousia-aletheia\nVersion: 2.0.1\n", encoding="utf-8")
    retired = home / "installs" / "i" / "environments" / "99999999aaaa" / "venv"
    retired_dist = retired / "lib" / "python3.12" / "site-packages" / "ousia_aletheia-1.9.9.dist-info"
    retired_dist.mkdir(parents=True)
    (retired_dist / "METADATA").write_text("Name: ousia-aletheia\nVersion: 1.9.9\n", encoding="utf-8")
    _facts(home, env)

    report = module.collect(repo, home)
    judged = {label: copy for label, copy in report["copies"].items() if copy.get("judged")}
    assert any(label.startswith("dist:") and copy["version"] == "2.0.1" for label, copy in judged.items()), \
        "the installed distribution of the resolved environment must be seen and judged"
    assert report["drift"] is True, "a stale dist in the environment the gateway runs is drift"
    retired_copies = {label for label, copy in report["copies"].items() if not copy.get("judged")}
    assert any(label.startswith("dist:99999999") for label in retired_copies), \
        "a retired environment is printed for information, not counted"


def test_verify_deployed_does_not_judge_an_editable_shims_version(tmp_path):
    """An editable dist-info carries the version it was installed at; the code is the source tree.

    That tree is what plugin_sources judges. Counting the shim as drift reports a stale number
    rather than different code — which is the state of the environment the gateway runs here.
    """
    module = _load(VERIFY, "verify_deployed_editable")
    repo = _repo_copy(tmp_path / "repo", version="2.1.0")
    (repo / "pyproject.toml").write_text(
        (ROOT / "pyproject.toml").read_text(encoding="utf-8"), encoding="utf-8")
    home = tmp_path / "home"
    _deployed(repo, home)
    fake = _fake_interpreter(tmp_path / "fake" / "python",
                             [_probe_payload({"mlx": True, "mlx_embeddings": True,
                                              "sentence_transformers": True})])
    env = home / "installs" / "i" / "environments" / "abcdef1234567890" / "venv"
    (env / "bin").mkdir(parents=True)
    os.link(fake, env / "bin" / "python")
    site = env / "lib" / "python3.12" / "site-packages"
    (site / "ousia_aletheia-2.0.1.dist-info").mkdir(parents=True)
    (site / "ousia_aletheia-2.0.1.dist-info" / "METADATA").write_text(
        "Name: ousia-aletheia\nVersion: 2.0.1\n", encoding="utf-8")
    (site / "__editable__.ousia_aletheia-2.0.1.pth").write_text(
        "import __editable___ousia_aletheia_2_0_1_finder\n", encoding="utf-8")
    _facts(home, env)

    report = module.collect(repo, home)
    dist_copies = {label: copy for label, copy in report["copies"].items() if label.startswith("dist:")}
    assert dist_copies and all(not copy.get("judged") for copy in dist_copies.values())
    assert report["drift"] is False, "the editable shim's version is not the deployed code"