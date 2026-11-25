"""History: every task and click that changes files is listed; Move back undoes moves only when it's safe (and
the matches follow); Restore brings things back from the Recycle Bin (a stand-in bin in Windows' own format)."""
import shutil
import struct
import sys
import tempfile
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mm import core, history, recyclebin, web  # noqa: E402

S = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="mm-test-"))
lib, st, binf = S / "libH", S / "stH", S / "binH"
for d in (lib, st, binf):
    shutil.rmtree(d, ignore_errors=True)
st.mkdir(parents=True)
binf.mkdir()
for name in ("CONFIG", "MATCHES", "RENAME_LOG", "LIBRARY", "EDITS", "TV_LIBRARY", "TV_MATCHES", "TV_CACHE", "TV_RENAME_LOG", "PROBES", "HISTORY"):
    setattr(core, name, st / f"{name.lower()}.json")
core.save_json(core.CONFIG, {"tmdb_key": "x", "folder": str(lib)})
core.ffmpeg_info = lambda p, timeout=60: ""
core.recycle_capacity = lambda p: None
ok = lambda cond, msg: print(("PASS " if cond else "FAIL ") + msg)


def fake_send2trash(p):
    """What Windows does: the item becomes $Rxxxxxx, and $Ixxxxxx records where it came from."""
    p = Path(p)
    n = f"{time.time_ns() % 10**6:06d}{p.suffix}"
    original = str(p.resolve()) + "\0"
    filetime = int((time.time() + recyclebin.EPOCH_DIFF) * 1e7)
    (binf / f"$I{n}").write_bytes(struct.pack("<qqqi", 2, 1, filetime, len(original)) + original.encode("utf-16-le"))
    shutil.move(str(p), str(binf / f"$R{n}"))


sys.modules["send2trash"] = types.SimpleNamespace(send2trash=fake_send2trash)
recyclebin.bin_folder = lambda drive: binf
c = web.app.test_client()


def run(url, data):
    r = c.post(url, json=data).get_json()
    while not (j := c.get(f"/api/job/{r['job']}").get_json())["finished"]:
        time.sleep(0.05)
    assert not j["error"], j["error"]
    return j["result"]


# Nothing is kept unless the app switched History on
lib.mkdir()
(lib / "Heat.1995.1080p.mkv").write_bytes(b"heat")
(lib / "Heat.1995.1080p.en.srt").write_bytes(b"sub")
items = run("/api/scan", {"folder": str(lib)})["items"]
core.save_json(core.MATCHES, {items[0]["id"]: {"tmdb_id": 949, "title": "Heat", "year": 1995, "status": "manual"}})
core.begin_activity("x", "x")
core.changed(lib / "a.nfo")
core.end_activity()
ok(not core.HISTORY.exists(), "History is only kept when the app has it switched on")
history.enabled = True

# A rename is listed, and moved back
pv = c.get("/api/rename/preview").get_json()
run("/api/rename/apply", {"ids": [p["id"] for p in pv["plans"]]})
new = lib / "Heat (1995)" / "Heat (1995).mkv"
h = c.get("/api/history").get_json()["entries"]
ok(new.exists() and h[0]["title"] == "Renamed movies" and h[0]["counts"].get("moved") == 2 and h[0]["can_move_back"],
   f"a rename is listed with what moved: {h and h[0]['title']} {h and h[0]['counts']}")
first = h[0]["id"]
r = c.post("/api/history/move-back", json={"id": first}).get_json()
matches = core.load_json(core.MATCHES, {})
ok(r["moved"] == 2 and (lib / "Heat.1995.1080p.mkv").exists() and (lib / "Heat.1995.1080p.en.srt").exists()
   and not (lib / "Heat (1995)").exists() and core.key_of(lib / "Heat.1995.1080p.mkv") in matches,
   "Move back puts the files back (empty folder removed), and the match follows")
ok(core.load_json(core.RENAME_LOG, []) == [], "the renamer's own Undo no longer offers what was moved back")
h = c.get("/api/history").get_json()["entries"]
ok(h[0]["title"] == "Moved back" and not h[1]["can_move_back"] and h[1]["moved_back"], "moving back is listed too, and can't be repeated")
r = c.post("/api/history/move-back", json={"id": first})
ok(r.status_code == 400 and "already" in r.get_json()["error"], "moving back twice is refused")

# Not safe: something is where a file would go back to
pv = c.get("/api/rename/preview").get_json()
run("/api/rename/apply", {"ids": [p["id"] for p in pv["plans"]]})
second = c.get("/api/history").get_json()["entries"][0]["id"]
(lib / "Heat.1995.1080p.mkv").write_bytes(b"someone else")
r = c.post("/api/history/move-back", json={"id": second})
ok(r.status_code == 400 and "Nothing was changed" in r.get_json()["error"] and new.exists(),
   "when something is in the way, nothing moves")
(lib / "Heat.1995.1080p.mkv").unlink()

# Deleting is listed, and restored from the Recycle Bin
items = run("/api/scan", {"folder": str(lib)})["items"]
d = c.post("/api/delete", json={"id": items[0]["id"], "mode": "files"}).get_json()
h = c.get("/api/history").get_json()["entries"]
ok(not new.exists() and h[0]["title"] == "Deleted a movie" and h[0]["restorable"] == 2, f"a delete is listed: {h[0]['title']} {h[0]['counts']}")
r = c.post("/api/history/restore", json={"id": h[0]["id"], "index": 0}).get_json()
ok(len(r["restored"]) == 1 and new.exists() and new.read_bytes() == b"heat", "one item restored to where it was")
r = c.post("/api/history/restore", json={"id": h[0]["id"]}).get_json()
ok(len(r["restored"]) == 1 and (lib / "Heat (1995)" / "Heat (1995).en.srt").exists() and not list(binf.iterdir()),
   "Restore all brings back the rest; the bin is left tidy")
r = c.post("/api/history/restore", json={"id": h[0]["id"]})
ok(r.status_code == 400 and "Nothing left" in r.get_json()["error"], "nothing is restored twice")
h = c.get("/api/history").get_json()["entries"]
ok(h[0]["title"] == "Restored from the Recycle Bin" and h[0]["counts"] == {"restored": 1}, "restoring is listed")

# Paging
for i in range(105):
    core.begin_activity("x", f"Test {i}")
    core.changed(lib / f"{i}.nfo")
    core.end_activity()
p1 = c.get("/api/history").get_json()
p2 = c.get(f"/api/history?before={p1['entries'][-1]['id']}").get_json()
ok(len(p1["entries"]) == 100 and p1["more"] and p1["entries"][0]["title"] == "Test 104" and p2["entries"][0]["title"] == "Test 4",
   "newest first, 100 at a time")
shutil.rmtree(S, ignore_errors=True)
print("done")
