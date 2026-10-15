"""Run every tests/test_*.py in its own process:  uv run python tests/run_all.py"""
import subprocess, sys
from pathlib import Path

failed = []
for t in sorted(Path(__file__).parent.glob("test_*.py")):
    r = subprocess.run([sys.executable, str(t)], capture_output=True, text=True)
    last = (r.stdout.strip().splitlines() or ["(no output)"])[-1]
    print(("PASS " if r.returncode == 0 else "FAIL "), t.name, "-", last)
    if r.returncode:
        failed.append(t.name); print(r.stdout[-1500:], r.stderr[-1500:])
sys.exit(1 if failed else 0)
