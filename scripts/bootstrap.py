#!/usr/bin/env python3
"""
One-command setup for a fresh clone.

    python scripts/bootstrap.py

1. creates a virtualenv in ./.venv (skipped if it exists)
2. installs requirements.txt
3. creates .env from .env.example (never overwrites an existing .env)
4. runs the offline setup check and tells you what to do next
"""
import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV = ROOT / ".venv"
BIN = VENV / ("Scripts" if os.name == "nt" else "bin")
PY = BIN / ("python.exe" if os.name == "nt" else "python")


def run(*cmd):
    print("  $", " ".join(str(c) for c in cmd))
    subprocess.check_call([str(c) for c in cmd], cwd=ROOT)


def main() -> int:
    if sys.version_info < (3, 9):
        print("Python 3.9+ is required.")
        return 1
    print("[1/4] Virtual environment")
    if not PY.exists():
        venv.create(VENV, with_pip=True)
    else:
        print("  already exists")
    print("[2/4] Installing dependencies")
    run(PY, "-m", "pip", "install", "--quiet", "--upgrade", "pip")
    run(PY, "-m", "pip", "install", "--quiet", "-r", "requirements.txt")
    print("[3/4] Configuration file")
    env, example = ROOT / ".env", ROOT / ".env.example"
    if env.exists():
        print("  .env already exists - left untouched")
    else:
        shutil.copyfile(example, env)
        print("  created .env from .env.example  <-- now edit it with your credentials")
    print("[4/4] Offline check")
    subprocess.call([str(PY), "scripts/doctor.py", "--offline"], cwd=ROOT)

    activate = r".venv\Scripts\activate" if os.name == "nt" else "source .venv/bin/activate"
    print(f"""
Next steps
  1. Edit .env (platform URL, manager, back-office admin, investor password)
  2. {activate}
  3. python scripts/doctor.py            # full check incl. login
  4. python dashboard/app.py             # then open http://localhost:5001
  Optional (browser-driven scripts): playwright install chromium
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
