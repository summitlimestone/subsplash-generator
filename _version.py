"""App version, shown in the GUI's title bars.

Stays "dev" in the repo and when running from source. packaging/build.py
overwrites this file's VERSION with the git tag name right before invoking
PyInstaller for a release build, so the built .exe reports its own version
without needing any packaging metadata at runtime.
"""

VERSION = "dev"
