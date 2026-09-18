"""Launch a Python command from a manifest without shell argument quoting."""
import json
import os
import subprocess
import sys


def main():
    manifest = os.environ.get("JARVIS_PYTHON_MANIFEST")
    if not manifest:
        return 64
    # Windows PowerShell 5.1 may emit a UTF-8 BOM for manifest writes.
    with open(manifest, encoding="utf-8-sig") as stream:
        argv = json.load(stream)
    if not isinstance(argv, list) or not all(isinstance(item, str) for item in argv):
        return 65
    completed = subprocess.run(argv, check=False)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
