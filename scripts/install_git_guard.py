#!/usr/bin/env python3
"""Turn on the leak guard for this clone: git runs scripts/leak_check.py before every commit, commit message and push.

    python scripts/install_git_guard.py

Run it once in every fresh clone or cloud session before touching real data. Safe to run again.
"""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOOKS = ("pre-commit", "commit-msg", "pre-push")


def main() -> int:
    for name in HOOKS:
        path = ROOT / "scripts" / "githooks" / name
        if not path.is_file():
            sys.exit(f"missing {path}")
        path.chmod(path.stat().st_mode | 0o111)
    subprocess.run(["git", "config", "core.hooksPath", "scripts/githooks"], cwd=str(ROOT), check=True)
    got = subprocess.run(["git", "config", "--get", "core.hooksPath"], cwd=str(ROOT), capture_output=True, text=True).stdout.strip()
    print(f"Leak guard on: git now runs {', '.join(HOOKS)} from {got} (never use --no-verify).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
