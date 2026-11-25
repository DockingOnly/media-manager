"""Jellyfin: the changed folders are reported (mapped to Jellyfin's paths), many become one scan, the key is checked.
Watching: new files are handled only after they stop growing; nothing is renamed; the page hears about it."""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mm import core, jellyfin, watch, web  # noqa: E402

S = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="mm-test-"))
lib, st = S / "libJ", S / "stJ"
for d in (lib, st):
    shutil.rmtree(d, ignore_errors=True)
st.mkdir(parents=True)
for name in ("CONFIG", "MATCHES", "RENAME_LOG", "LIBRARY", "EDITS", "TV_LIBRARY", "TV_MATCHES", "TV_CACHE", "TV_RENAME_LOG"):
    setattr(core, name, st / f"{name.lower()}.json")
core.save_json(core.CONFIG, {"tmdb_key": "x", "folder": str(lib)})
core.ffmpeg_info = lambda p, timeout=60: ""
ok = lambda cond, msg: print(("PASS " if cond else "FAIL ") + msg)

# ---------------------------------------------------------------- a stand-in Jellyfin
got = []


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, data=None):
        raw = json.dumps(data).encode() if data is not None else b""
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def handle_any(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n)) if n else None
        if 'Token="good-key-1234"' not in (self.headers.get("Authorization") or ""):
            return self._send(401)
        got.append((self.command, self.path, body))
        if self.path == "/System/Info":
            return self._send(200, {"ServerName": "Den", "Version": "10.10.7"})
        if self.path == "/Library/VirtualFolders":
            return self._send(200, [{"Name": "Movies", "CollectionType": "movies", "Locations": ["/media/Movies"]}])
        return self._send(204)

    do_GET = do_POST = handle_any


srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
threading.Thread(target=srv.serve_forever, daemon=True).start()
URL = f"http://127.0.0.1:{srv.server_address[1]}"
c = web.app.test_client()

ok(jellyfin.to_jellyfin(r"D:\Media\Movies\Heat (1995)", "D:\\Media => /media") == "/media/Movies/Heat (1995)"
   and jellyfin.to_jellyfin(r"E:\Other\x", "D:\\Media => /media") == r"E:\Other\x", "paths are mapped to how Jellyfin sees them")

r = c.post("/api/settings", json={"jellyfin_url": URL, "jellyfin_key": "bad", "jellyfin_map": ""})
ok(r.status_code == 400 and "refused the API key" in r.get_json()["error"] and not core.load_config().get("jellyfin_key"),
   "a key Jellyfin refuses isn't saved")
r = c.post("/api/settings", json={"jellyfin_url": URL + "/", "jellyfin_key": "good-key-1234", "jellyfin_map": f"{lib} => /media/Movies"}).get_json()
cfg = core.load_config()
ok(cfg["jellyfin_key"] == "good-key-1234" and cfg["jellyfin_url"] == URL and "Connected to Den (Jellyfin 10.10.7)" in r["jellyfin_info"]
   and "good-key" not in json.dumps(r), "a working address and key are checked and saved; the page only sees a hint of the key")
t = c.post("/api/jellyfin/test", json={}).get_json()
ok(not t["warnings"] and t["libraries"][0]["name"] == "Movies", "your movie folder is inside a Jellyfin library (through the mapping)")
t = c.post("/api/jellyfin/test", json={"map": ""}).get_json()
ok(len(t["warnings"]) == 1 and "isn't in any Jellyfin library" in t["warnings"][0], "without the mapping, the test says Jellyfin can't see the folder")

got.clear()
jellyfin.QUIET_FOR = 0
core.take_changes()
core.changed(lib / "Heat (1995)" / "Heat (1995).nfo", lib / "Heat (1995)" / "poster.jpg", lib / "Up (2009)" / "Up (2009).en.srt")
jellyfin.flush_once()
upd = [b for m, p, b in got if p == "/Library/Media/Updated"]
ok(len(upd) == 1 and sorted(u["Path"] for u in upd[0]["Updates"]) == ["/media/Movies/Heat (1995)", "/media/Movies/Up (2009)"],
   f"changed files are reported once per folder, as Jellyfin sees them: {upd}")
got.clear()
core.changed(*[lib / f"Movie {i}" / "x.nfo" for i in range(jellyfin.FULL_SCAN_ABOVE)])
jellyfin.flush_once()
ok([p for m, p, b in got] == ["/Library/Refresh"], "many folders at once become one library scan")
got.clear()
jellyfin.QUIET_FOR = 60
core.changed(lib / "a" / "b.nfo")
jellyfin.flush_once()
ok(not got, "nothing is sent while changes are still coming in")
jellyfin.QUIET_FOR = 0
c.post("/api/settings", json={"jellyfin_url": URL, "jellyfin_key": "", "jellyfin_map": f"{lib} => /media/Movies", "jellyfin_auto": False})
jellyfin.flush_once()
ok(not got and core.take_changes() == [], "with telling Jellyfin turned off, nothing is sent (and nothing piles up)")

# ---------------------------------------------------------------- watching
(lib / "Heat (1995)").mkdir(parents=True)
(lib / "Heat (1995)" / "Heat (1995).mkv").write_bytes(b"heat")
r = c.post("/api/scan", json={"folder": str(lib)}).get_json()
while not c.get(f"/api/job/{r['job']}").get_json()["finished"]:
    time.sleep(0.05)
core.save_config(watch_on=True, watch_steps=[], watch_minutes=5)
new = lib / "Dune.Part.Two.2024.1080p.WEB.mkv"
new.write_bytes(b"part")
now = time.time()
ok(watch.check_once(c, now) == [] and watch.state["waiting"] == [new.name], "a new file is first seen as still arriving")
new.write_bytes(b"partpart")
ok(watch.check_once(c, now + 120) == [] and watch.state["waiting"], "a file that grew since the last look keeps waiting")
old = time.time() - 600
os.utime(new, (old, old))
watch.check_once(c, now + 120)
res = watch.check_once(c, now + 240)
ok(len(res) == 1 and res[0]["added"] and "Dune" in res[0]["added"][0] and res[0]["steps"] == ["scanned"] and res[0]["unmatched"] == 1,
   f"once it stops growing, it's scanned in: {res}")
ok(new.exists() and (lib / "Heat (1995)" / "Heat (1995).mkv").exists(), "nothing is renamed or moved")
ok(watch.check_once(c, now + 360) == [], "nothing new next time: nothing runs")
p = c.post("/api/ping").get_json()
ok(p["watch"]["version"] >= 1 and "1 new movie file: Dune" in p["watch"]["message"],
   f"the page hears what arrived: {p['watch']['message']}")
(lib / "Heat (1995)" / "Heat (1995).mkv").unlink()
res = watch.check_once(c, now + 480)
ok(len(res) == 1 and res[0]["gone"] == 1 and "no longer there" in watch.describe(res[0]), "a file that's gone is noticed too")

# TV: a new episode is scanned in; a step that fails (TMDB unreachable here) doesn't lose the scan
tv = S / "tvJ"
(tv / "Show (2001)" / "Season 1").mkdir(parents=True)
(tv / "Show (2001)" / "Season 1" / "Show - S01E01 - One.mkv").write_bytes(b"one")
core.save_config(tv_folder=str(tv), watch_steps=["match"])
r = c.post("/api/tv/scan", json={"folder": str(tv)}).get_json()
while not c.get(f"/api/job/{r['job']}").get_json()["finished"]:
    time.sleep(0.05)
ep2 = tv / "Show (2001)" / "Season 1" / "Show - S01E02 - Two.mkv"
ep2.write_bytes(b"two")
os.utime(ep2, (old, old))
watch.check_once(c, now + 600)
v = watch.state["version"]
res = watch.check_once(c, now + 720)
ok(len(res) == 1 and res[0]["kind"] == "tv" and res[0]["added"] == ["Show S01E02"] and res[0]["steps"] == ["scanned"]
   and res[0]["problem"] and watch.state["version"] == v + 1 and "tried again next time" in watch.describe(res[0]),
   f"a new episode is scanned in, and a failed step is reported without losing the scan: {res and watch.describe(res[0])}")
srv.shutdown()
shutil.rmtree(S, ignore_errors=True)
print("done")
