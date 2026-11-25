"""Saving and reading the same file at once (two tabs open, antivirus, backups) never fails, and a read never comes
back empty just because the file was busy: saving after an 'empty' read would wipe your settings."""
import shutil
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mm import core  # noqa: E402

S = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="mm-test-"))
S.mkdir(parents=True, exist_ok=True)
f = S / "config.json"
core.save_json(f, {"tmdb_key": "keep-me", "n": -1})
ok = lambda cond, msg: print(("PASS " if cond else "FAIL ") + msg)
errors, empty = [], []
EMPTY = object()


def writer(n):
    for i in range(200):
        try:
            core.save_json(f, {"tmdb_key": "keep-me", "n": n * 1000 + i, "pad": "x" * 3000})
        except OSError as e:
            errors.append(repr(e))


def reader():
    for _ in range(800):
        try:
            d = core.load_json(f, EMPTY)
            if d is EMPTY or d.get("tmdb_key") != "keep-me":
                empty.append(d)
        except OSError as e:
            errors.append(repr(e))


threads = [threading.Thread(target=writer, args=(n,)) for n in range(3)] + [threading.Thread(target=reader) for _ in range(3)]
for t in threads:
    t.start()
for t in threads:
    t.join()
ok(not errors, f"no errors while three saves and three reads run at once ({len(errors)}: {errors[:2]})")
ok(not empty, f"a read never comes back empty or partial ({len(empty)})")
ok(core.load_json(f, {})["tmdb_key"] == "keep-me" and not list(S.glob("*.tmp")), "the file is whole afterwards, and no temporary copies are left")
ok(core.load_json(S / "missing.json", {"d": 1}) == {"d": 1}, "a file that doesn't exist still gives the default")
shutil.rmtree(S, ignore_errors=True)
print("done")
