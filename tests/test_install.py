import subprocess
import sys
import os
import shutil
import tempfile

WHEEL = None
VENV = "/tmp/aletheia-install-test-pytest"


def _find_wheel():
    global WHEEL
    if WHEEL and os.path.exists(WHEEL):
        return WHEEL
    dist_dir = os.path.join(os.path.dirname(__file__), "..", "dist")
    for name in os.listdir(dist_dir):
        if name.endswith(".whl"):
            WHEEL = os.path.join(dist_dir, name)
            return WHEEL
    raise FileNotFoundError("no wheel in dist/")


def _python():
    # Match build-release.sh / verify-install.sh selection logic.
    if os.environ.get("ALETHEIA_PYTHON"):
        return os.environ["ALETHEIA_PYTHON"]
    for candidate in ("python3.11", "python3.12", "python3.13", "python3"):
        if shutil.which(candidate):
            return candidate
    raise FileNotFoundError("no python3.11+ found")


def test_install_uninstall_cycle():
    wheel = _find_wheel()
    python = _python()
    venv = tempfile.mkdtemp(prefix="aletheia-install-test-")
    old_umask = os.umask(0o077)
    try:
        subprocess.run([python, "-m", "venv", venv], check=True)
        pip = os.path.join(venv, "bin", "python")
        subprocess.run([pip, "-m", "pip", "install", "--quiet", wheel], check=True)
    finally:
        os.umask(old_umask)
    aletheia = os.path.join(venv, "bin", "aletheia")
    subprocess.run([aletheia, "--help"], check=True, stdout=subprocess.DEVNULL)
    db = "/tmp/aletheia-install-pytest.db"
    subprocess.run([aletheia, "--db", db, "serve", "--help"], check=True, stdout=subprocess.DEVNULL)
    subprocess.run([pip, "-m", "pip", "uninstall", "-y", "ousia-aletheia"], check=True)
    assert not os.path.exists(aletheia), "aletheia entrypoint still exists after uninstall"
    # Run import check from a neutral directory to avoid cwd shadowing.
    result = subprocess.run(
        [pip, "-c", "import sys; sys.path = [p for p in sys.path if p]; import aletheia"],
        cwd="/tmp",
        capture_output=True,
    )
    assert result.returncode != 0, "aletheia still importable after uninstall"
    shutil.rmtree(venv)
