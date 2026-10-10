"""Build the distributions and install the wheel into a clean virtual environment.

On the Windows CI runners this is the Windows packaging test: the wheel is installed
with its platform dependencies (libusb-package) and the console entry point is run.
"""

from __future__ import annotations

import json
import subprocess
import sys
import venv
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.slow


@pytest.fixture(scope="module")
def dist(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("dist")
    subprocess.run(
        [sys.executable, "-m", "build", "--outdir", str(out), str(REPO)],
        check=True,
        capture_output=True,
        timeout=600,
    )
    return out


def test_wheel_contents(dist: Path) -> None:
    wheel = next(dist.glob("hantek_mcp-*.whl"))
    names = zipfile.ZipFile(wheel).namelist()
    assert "hantek_mcp/protocol/data/dso5102p_fw3.40.0/protocol.inf" in names
    assert "hantek_mcp/protocol/data/dso5102p_fw3.40.0/keyprotocol.inf" in names
    assert "hantek_mcp/py.typed" in names
    assert not any(n.startswith("tests/") for n in names)
    meta = next(n for n in names if n.endswith("entry_points.txt"))
    ep = zipfile.ZipFile(wheel).read(meta).decode()
    assert "hantek-mcp = hantek_mcp.cli:main" in ep and "hantek-mcp-gui" in ep
    md = zipfile.ZipFile(wheel).read(next(n for n in names if n.endswith("METADATA"))).decode()
    assert "libusb-package" in md and "sys_platform == 'win32'" in md
    assert any(n.endswith("LICENSE") for n in names)
    assert next(dist.glob("hantek_mcp-*.tar.gz"))


def test_wheel_installs_and_runs_in_clean_venv(dist: Path, tmp_path: Path) -> None:
    wheel = next(dist.glob("hantek_mcp-*.whl"))
    env_dir = tmp_path / "venv"
    venv.EnvBuilder(with_pip=True).create(env_dir)
    bindir = env_dir / ("Scripts" if sys.platform == "win32" else "bin")
    py = bindir / ("python.exe" if sys.platform == "win32" else "python")
    subprocess.run([str(py), "-m", "pip", "install", "-q", str(wheel)], check=True, timeout=900)
    exe = bindir / ("hantek-mcp.exe" if sys.platform == "win32" else "hantek-mcp")
    r = subprocess.run(
        [str(exe), "capture", "--simulate", "--data-dir", str(tmp_path / "d"), "--measure", "frequency"],
        capture_output=True,
        text=True,
        timeout=300,
        check=True,
    )
    out = json.loads(r.stdout)
    assert out["capture"]["simulated"]
    fr = out["measurements"]["frequency"]["results"]
    assert abs(fr["frequency_hz"] - 1000) / 1000 <= 3 * fr["relative_uncertainty_est"]
    r = subprocess.run([str(exe), "diagnose"], capture_output=True, text=True, timeout=120, check=True)
    diag = json.loads(r.stdout)["usb"]
    if sys.platform == "win32":
        assert diag.get("backend"), diag  # bundled libusb DLL must load on Windows
