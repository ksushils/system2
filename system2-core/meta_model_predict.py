#!/usr/bin/env python3
import os
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parent
python = root / ".meta-venv" / "bin" / "python"
if not python.exists():
    python = Path(sys.executable)
raise SystemExit(subprocess.call([str(python), str(root / "meta_model.py"), *sys.argv[1:]], env=os.environ.copy()))
