#!/usr/bin/env python3
"""Builds SermonMarker.exe: marker.py as a single windowed executable with
ffmpeg, ffprobe, ffplay (and any DLLs next to them) bundled inside.

    pip install pyinstaller
    python packaging/build_marker.py <ffmpeg bin folder>

The ffmpeg bin folder is the `bin` directory of a Windows ffmpeg build
(the CI job uses BtbN's win64-lgpl-shared build). Output:
dist/SermonMarker.exe
"""

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    ffmpeg_bin = Path(sys.argv[1]).resolve()
    missing = [n for n in ("ffmpeg.exe", "ffprobe.exe", "ffplay.exe") if not (ffmpeg_bin / n).is_file()]
    if missing:
        sys.exit(f"{ffmpeg_bin} is missing {', '.join(missing)}")

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--onefile", "--windowed",
        "--name", "SermonMarker",
        "--distpath", str(REPO_ROOT / "dist"),
        "--workpath", str(REPO_ROOT / "build"),
        "--specpath", str(REPO_ROOT / "build"),
    ]
    # Unpacked to the root of the runtime folder, which marker.py puts first
    # on PATH.
    for f in sorted(ffmpeg_bin.iterdir()):
        if f.suffix.lower() in (".exe", ".dll"):
            cmd += ["--add-binary", f"{f}{';' if sys.platform == 'win32' else ':'}."]
    cmd.append(str(REPO_ROOT / "marker.py"))
    print("[build]", " ".join(cmd))
    subprocess.run(cmd, cwd=REPO_ROOT, check=True)
    print("[build] done:", REPO_ROOT / "dist" / "SermonMarker.exe")


if __name__ == "__main__":
    main()
