"""Run every test file and summarise. Usage (from the app folder): .venv/Scripts/python tests/run_all.py"""
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
total_pass = total_fail = 0
for test in sorted(HERE.glob("test_*.py")):
    out = subprocess.run([sys.executable, str(test)], capture_output=True, text=True, encoding="utf-8", errors="replace")
    lines = (out.stdout + out.stderr).splitlines()
    passed = sum(1 for l in lines if l.startswith("PASS"))
    failed = [l for l in lines if l.startswith("FAIL")]
    crashed = out.returncode != 0 or any(l.startswith("Traceback") for l in lines)
    skipped = any(l.startswith("SKIP") for l in lines)
    total_pass += passed
    total_fail += len(failed) + (1 if crashed else 0)
    status = "skipped" if skipped else ("CRASHED" if crashed else f"{passed} passed" + (f", {len(failed)} failed" if failed else ""))
    print(f"{test.name:28} {status}")
    for l in failed:
        print("   " + l)
    if crashed:
        print("\n".join("   " + l for l in lines[-12:]))
print(f"\n{total_pass} passed, {total_fail} failed")
sys.exit(1 if total_fail else 0)
