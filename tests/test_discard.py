"""'To the Recycle Bin' must always be restorable: Windows silently deletes for good anything the bin can't take
(paths over 260 characters, or more than the bin holds). From a real case: a 325-character path inside
tinyMediaManager's trash folder."""
import shutil
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mm import core  # noqa: E402

S = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="mm-test-"))
ok = lambda cond, msg: print(("PASS " if cond else "FAIL ") + msg)
sent = []


def fake_send2trash(p):
    sent.append(p)
    shutil.rmtree(p) if Path(p).is_dir() else Path(p).unlink()


sys.modules["send2trash"] = types.SimpleNamespace(send2trash=fake_send2trash)
core.is_network_path = lambda p: False
core.recycle_capacity = lambda p: 10**12

deep = S / "LongFolderName" / ("x" * 40) / ("y" * 40)
deep.mkdir(parents=True)
(deep / "movie.mkv").write_bytes(b"12345")
longest = core.tree_stats(S / "LongFolderName")[1]
core.MAX_BIN_PATH = longest - 5  # pretend this tree is too long for the bin, but short enough once moved up

core.discard(S / "LongFolderName")
ok(len(sent) == 1 and len(sent[0]) < len(str(S / "LongFolderName")) and "~mm-recycle-" in sent[0] and not (S / "LongFolderName").exists(),
   f"a too-long folder is first moved to a short name at the top of the drive, then recycled ({sent[0] if sent else ''})")

(S / "Keep").mkdir()
(S / "Keep" / ("z" * 60 + ".mkv")).write_bytes(b"0123456789")
core.MAX_BIN_PATH = 20  # too long even after moving up
try:
    core.discard(S / "Keep")
    ok(False, "refused when even the short name is too long")
except core.UserError as err:
    ok("too long for the Recycle Bin" in str(err) and (S / "Keep").exists() and len(sent) == 1, "refused when even the short name is too long; nothing touched")

core.MAX_BIN_PATH = 259
core.recycle_capacity = lambda p: 3
try:
    core.discard(S / "Keep")
    ok(False, "refused when bigger than the Recycle Bin")
except core.UserError as err:
    ok("more than this drive's Recycle Bin holds" in str(err) and (S / "Keep").exists(), "refused when bigger than the Recycle Bin; nothing touched")
core.recycle_capacity = lambda p: 0
try:
    core.discard(S / "Keep")
    ok(False, "refused when the Recycle Bin is off")
except core.UserError:
    ok((S / "Keep").exists(), "refused when the Recycle Bin is switched off for the drive")
shutil.rmtree(S, ignore_errors=True)
print("done")
