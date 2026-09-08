#!/usr/bin/env python3
"""Build the Windows executables (SubsplashGenerator.exe + service_video.exe)
with PyInstaller.

Run on Windows, from the repo root or anywhere:
    pip install -r requirements.txt
    pip install pyinstaller
    python packaging/build.py [version]

`version` (optional) becomes the string shown in the GUI's title bar and
the release zip's name — e.g. a git tag like "v1.2.0". Defaults to the
APP_VERSION environment variable, or "dev" if neither is set. This is the
same script the release-windows.yml CI workflow runs.

Output: two onefile executables in dist/ —
    dist/SubsplashGenerator.exe   the Tkinter GUI (gui.py), windowed
    dist/service_video.exe        the CLI (service_video.py), console

Both are built as PyInstaller "onefile" executables so there's nothing
else to install, and they're built separately (not combined into one
exe) because gui.py spawns service_video.py as a real subprocess rather
than calling it in-process — see service_command() in gui.py. Ship them
in the same folder along with ffmpeg.exe/ffprobe.exe (not built here;
see release-windows.yml, which downloads a static build) and both
executables will find each other and ffmpeg via that folder alone, no
PATH setup needed.
"""

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Packages whose imports PyInstaller's static analysis tends to miss
# because they load submodules dynamically at runtime (FastAPI/Starlette
# and Uvicorn both do this heavily) — used only by the optional built-in
# API server (config.json's "api.enabled"), but it has to still work in a
# frozen build if someone turns that on.
COLLECT_ALL = ["fastapi", "starlette", "uvicorn", "obsws_python", "websockets"]


def write_version(version: str) -> None:
    version_file = REPO_ROOT / "_version.py"
    version_file.write_text(
        '"""App version, shown in the GUI\'s title bars. Written by packaging/build.py."""\n\n'
        f'VERSION = "{version}"\n'
    )
    print(f"[build] _version.py -> VERSION = {version!r}")


def run_pyinstaller(args: list[str]) -> None:
    cmd = [sys.executable, "-m", "PyInstaller", *args]
    print(f"[build] {' '.join(cmd)}")
    subprocess.run(cmd, cwd=REPO_ROOT, check=True)


def main() -> None:
    version = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("APP_VERSION", "dev")
    write_version(version)

    common = [
        "--noconfirm",
        "--onefile",
        "--distpath", "dist",
        "--workpath", "build",
        "--specpath", "build",
    ]
    for pkg in COLLECT_ALL:
        common += ["--collect-all", pkg]

    # service_video.py first: a plain console CLI, so its stdout/stderr
    # behave normally both piped (from gui.py) and run directly in a
    # terminal.
    run_pyinstaller([
        *common,
        "--name", "service_video",
        "--console",
        str(REPO_ROOT / "service_video.py"),
    ])

    # gui.py: windowed, so no console box pops up alongside the Tk window.
    run_pyinstaller([
        *common,
        "--name", "SubsplashGenerator",
        "--windowed",
        str(REPO_ROOT / "gui.py"),
    ])

    print("[build] done — dist/SubsplashGenerator.exe + dist/service_video.exe")


if __name__ == "__main__":
    main()
