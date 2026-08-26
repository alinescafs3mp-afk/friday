"""The installed service must carry the Admin UI it serves."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def test_real_wheel_ignores_a_stale_manifest_and_carries_runtime_assets(tmp_path):
    """Build the artifact users install, including UI and host-side entrypoints."""
    project = tmp_path / "project"
    for package in ("friday", "friday_host_agent", "friday_package_broker"):
        shutil.copytree(
            REPO / package,
            project / package,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
    for name in ("pyproject.toml", "README.md", "LICENSE"):
        shutil.copy2(REPO / name, project / name)

    # Setuptools normally carries this ignored cache across builds.  One tool
    # invocation with an absolute script_name used to poison it permanently and
    # make every later ``python -m build`` fail before producing a wheel.
    egg_info = project / "friday.egg-info"
    egg_info.mkdir()
    (egg_info / "SOURCES.txt").write_text(
        f"pyproject.toml\n{project / 'pyproject.toml'}\n",
        encoding="utf-8",
    )

    completed = subprocess.run(
        (
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--no-isolation",
            "--outdir",
            str(project / "dist"),
        ),
        cwd=project,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr

    wheels = list((project / "dist").glob("*.whl"))
    assert len(wheels) == 1
    wheel_digest = hashlib.sha256(wheels[0].read_bytes()).hexdigest()
    verified = subprocess.run(
        [
            sys.executable,
            str(REPO / "deploy" / "host-control" / "verify_wheel.py"),
            str(wheels[0]),
            wheel_digest,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert verified.returncode == 0, verified.stderr
    assert f"sha256:{wheel_digest}" in verified.stdout
    with zipfile.ZipFile(wheels[0]) as wheel:
        packaged = set(wheel.namelist())
        entry_points = [name for name in packaged if name.endswith(".dist-info/entry_points.txt")]
        assert len(entry_points) == 1
        entry_point_text = wheel.read(entry_points[0]).decode("utf-8")
        licenses = [name for name in packaged if name.endswith(".dist-info/licenses/LICENSE")]
        assert len(licenses) == 1
        assert wheel.read(licenses[0]) == (REPO / "LICENSE").read_bytes()

    assert {
        "friday/admin_ui/static/index.html",
        "friday/admin_ui/static/app.js",
        "friday/admin_ui/static/app.css",
        # Панель без раскладки открывается, но граф в ней падает на первом же
        # обращении к `FridayGraphLayout`. Файл подключает страница, а не `app.js`,
        # поэтому сборщик о нём иначе не узнаёт.
        "friday/admin_ui/static/graph-layout.js",
        "friday_host_agent/__main__.py",
        "friday_host_agent/daemon.py",
        "friday_package_broker/__main__.py",
        "friday_package_broker/daemon.py",
    } <= packaged

    assert "friday-host-agent = friday_host_agent.__main__:main" in entry_point_text
    assert "friday-package-broker = friday_package_broker.__main__:main" in entry_point_text
