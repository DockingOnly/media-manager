"""Power-user features: keys in Credential Manager (a stand-in here, never the real one), daily backups and
restoring them, a quiet log, the extra table details (runtime, HDR, audio, rating...), notifications, Play and
Show in Jellyfin."""
import logging
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mm import backup, core, details, jellyfin, logs, notify, vault, web  # noqa: E402

S = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="mm-test-"))
lib, st = S / "libP", S / "stP"
for d in (lib, st):
    shutil.rmtree(d, ignore_errors=True)
st.mkdir(parents=True)
for name in ("CONFIG", "MATCHES", "RENAME_LOG", "LIBRARY", "EDITS", "TV_LIBRARY", "TV_MATCHES", "TV_CACHE", "TV_RENAME_LOG", "PROBES", "HISTORY"):
    setattr(core, name, st / f"{name.lower()}.json")
core.BACKUPS = st / "backups"
FFMPEG = """Input #0, matroska,webm, from 'x.mkv':
  Duration: 01:55:00.00, start: 0.000000, bitrate: 8000 kb/s
  Stream #0:0(eng): Video: hevc (Main 10), yuv420p10le(tv, bt2020nc/bt2020/smpte2084), 3840x2160, SAR 1:1 DAR 16:9, 23.98 fps
  Stream #0:1(eng): Audio: truehd (Atmos), 48000 Hz, 7.1, s32 (24 bit)
  Stream #0:2(jpn): Audio: aac (LC), 48000 Hz, stereo, fltp"""
core.ffmpeg_info = lambda p, timeout=60: FFMPEG
core.recycle_capacity = lambda p: None
ok = lambda cond, msg: print(("PASS " if cond else "FAIL ") + msg)
c = web.app.test_client()

# ---------------------------------------------------------------- keys in Credential Manager (a stand-in)
fake_vault = {}
vault._read = lambda name: fake_vault.get(name)
vault._write = lambda name, value: fake_vault.__setitem__(name, value) if value else fake_vault.pop(name, None)
core.save_json(core.CONFIG, {"tmdb_key": "secret-tmdb-1234", "folder": str(lib), "language": "en-US"})
ok(core.load_config()["tmdb_key"] == "secret-tmdb-1234" and not fake_vault, "with the vault off (as in tests), keys stay in config.json")
vault.enabled = True
if vault.available():
    core.save_config()  # what the app does at start
    raw = core.load_json(core.CONFIG, {})
    ok("tmdb_key" not in raw and fake_vault.get("tmdb_key") == "secret-tmdb-1234" and raw["language"] == "en-US",
       "starting the app moves keys out of config.json into Credential Manager")
    ok(core.load_config()["tmdb_key"] == "secret-tmdb-1234", "the rest of the app still reads them as before")
    core.save_config(jellyfin_key="jf-9999", language="de-DE")
    raw = core.load_json(core.CONFIG, {})
    ok("jellyfin_key" not in raw and fake_vault["jellyfin_key"] == "jf-9999" and raw["language"] == "de-DE",
       "saving a new key puts it in Credential Manager; other settings stay in config.json")
    v = c.get("/api/config").get_json()
    ok(v["keys_in_vault"] and v["has_key"] and v["key_hint"] == "1234" and "secret-tmdb-1234" not in str(v),
       "Settings says where the keys are, and never sends them to the page")
else:
    print("PASS (skipped: Credential Manager is Windows only)")

# ---------------------------------------------------------------- backups
core.save_json(core.MATCHES, {"a": {"tmdb_id": 1, "title": "One", "status": "manual"}})
core.save_json(core.HISTORY, [{"id": "h1", "title": "x", "items": []}])
ok(backup.daily() is None and not core.BACKUPS.exists(), "no backups unless the app switched them on")
backup.enabled = True
first = backup.daily()
ok(first and first["reason"] == "Daily" and "Movie matches" in first["what"] and "Settings" in first["what"], "the first start of the day makes a backup")
ok(backup.daily() is None, "only one daily backup a day")
saved_cfg = core.load_json(core.BACKUPS / first["id"] / core.CONFIG.name, {})
ok("tmdb_key" not in saved_cfg and "jellyfin_key" not in saved_cfg and saved_cfg.get("language") == "de-DE", "API keys are never in a backup")
core.save_json(core.MATCHES, {"b": {"tmdb_id": 2, "title": "Two", "status": "manual"}})
core.save_config(language="fr-FR")
r = c.post("/api/backups/restore", json={"id": first["id"]}).get_json()
ok(core.load_json(core.MATCHES, {}).keys() == {"a"} and core.load_config()["language"] == "de-DE",
   "Restore puts the matches and settings back")
ok(core.load_config().get("tmdb_key") == "secret-tmdb-1234", "restoring settings keeps the keys you have now")
undo = next(b for b in r["backups"] if b["id"] == r["undo"])
ok(undo["reason"] == "Before a restore" and core.load_json(core.BACKUPS / undo["id"] / core.MATCHES.name, {}).keys() == {"b"},
   "what you had before a restore is backed up first, so it can be undone")
r = c.post("/api/backups/restore", json={"id": "../../etc"})
ok(r.status_code == 400, "only the app's own backups can be restored")
for _ in range(backup.KEEP + 3):
    backup.make("manual")
ok(len(backup.listing()) == backup.KEEP, f"only the newest {backup.KEEP} are kept")
backup.enabled = False

# ---------------------------------------------------------------- a quiet log
q = logs.Quiet()
line = lambda req, code: logging.LogRecord("werkzeug", 20, "", 0, '%s - - [x] "%s" %s -', ("127.0.0.1", req, code), None)
ok(not q.filter(line("\x1b[33mPOST /api/ping HTTP/1.1\x1b[0m", "200")) and not q.filter(line("GET /api/job/scan-1 HTTP/1.1", "200"))
   and not q.filter(line("GET / HTTP/1.1", "304")), "the page's check-ins and pages loading fine are left out of the window")
ok(q.filter(line("POST /api/rename/apply HTTP/1.1", "200")) and q.filter(line("GET /api/movie?id=x HTTP/1.1", "500"))
   and q.filter(line("POST /api/ping HTTP/1.1", "500")), "actions and anything that failed are kept")

# ---------------------------------------------------------------- the extra table details
lib.mkdir()
(lib / "Heat (1995)").mkdir()
(lib / "Heat (1995)" / "Heat (1995).mkv").write_bytes(b"heat")
(lib / "Heat (1995)" / "Heat (1995).nfo").write_text(
    "<?xml version='1.0'?><movie><title>Heat</title><year>1995</year><mpaa>US:R / US:Rated R</mpaa><genre>Crime</genre>"
    "<genre>Drama</genre><set><name>Heist Films</name></set><ratings><rating name='themoviedb'><value>7.9</value></rating>"
    "<rating name='imdb'><value>8.3</value></rating></ratings><tag>heist</tag></movie>", encoding="utf-8")
core.save_config(folder=str(lib))
items = c.post("/api/scan", json={"folder": str(lib)}).get_json()
while not (j := c.get(f"/api/job/{items['job']}").get_json())["finished"]:
    time.sleep(0.05)
info = j["result"]["items"][0]["info"]
ok(info["runtime"] == 115 and info["hdr"] == "HDR10" and info["audio"] == "TrueHD Atmos 7.1" and info["audio_langs"] == ["en", "ja"]
   and info["bitrate"] == 8000 and info["resolution"] == "2160p", f"runtime, HDR, best audio and languages come from the file: {info}")
ok(info["rating"] == 8.3 and info["genres"] == ["Crime", "Drama"] and info["set"] == "Heist Films" and info["mpaa"] == "R" and info["tags"] == ["heist"],
   "rating (IMDb first), genres, collection, a short age rating and tags come from the .nfo")
ok(details.audio_name({"codec": "dts", "profile": "DTS-HD MA", "channels": 8}) == "DTS-HD MA 7.1", "DTS-HD MA is named as such")

# ---------------------------------------------------------------- Play, Show in Jellyfin
mid = j["result"]["items"][0]["id"]
played = []
web.os.startfile = lambda p: played.append(p)
r = c.post("/api/play", json={"id": mid}).get_json()
ok(played and played[0].endswith("Heat (1995).mkv") and r["name"] == "Heat (1995).mkv", "Play opens the movie in your usual player")
asked = []


def fake_request(method, path, body=None, **kw):
    asked.append(path)
    if path == "/Users":
        return [{"Id": "u1", "Policy": {"IsAdministrator": True}}]
    return {"Items": [{"Id": "other", "Name": "Heat", "Path": "D:\\elsewhere\\Heat.mkv"},
                      {"Id": "it42", "Name": "Heat", "ServerId": "srv", "Path": str(lib / "Heat (1995)" / "Heat (1995).mkv")}]}


jellyfin.request = fake_request
core.save_config(jellyfin_url="http://jf.local:8096")
r = c.post("/api/jellyfin/find", json={"id": mid}).get_json()
ok(r.get("url") == "http://jf.local:8096/web/#/details?id=it42&serverId=srv", f"Show in Jellyfin finds the right copy by its path: {r}")

# ---------------------------------------------------------------- notifications
x = notify.toast_xml('New <movie> & "more"', "a < b", "http://127.0.0.1:8765")
ok("&lt;movie&gt; &amp;" in x and 'launch="http://127.0.0.1:8765"' in x and "<text>a &lt; b</text>" in x, "notification text is escaped, and clicking opens the app")
ok(not notify.show("t", "x"), "no pop-ups unless the app switched them on (so never from tests)")
shown = []
notify.enabled = True
notify.show = lambda t, b: shown.append((t, b)) or True
core.save_config(watch_notify=True)
ok(notify.watch_result({"kind": "movies", "added": ["Heat (1995)", "Ran (1985)"], "steps": ["scanned", "match"], "unmatched": 0, "problem": None})
   and shown[0][0] == "2 new movies handled" and "Heat (1995)" in shown[0][1], "the watcher's work pops up a notification")
ok(not notify.watch_result({"kind": "movies", "added": [], "steps": ["scanned"], "unmatched": 0, "problem": None}), "nothing new: no notification")
core.save_config(watch_notify=False)
ok(not notify.watch_result({"kind": "tv", "added": ["X S01E01"], "steps": [], "unmatched": 0, "problem": None}), "switched off in Settings: no notification")
r = c.post("/api/settings", json={"watch": {"on": False, "minutes": 10, "notify": True, "steps": []}}).get_json()
ok(r["watch_notify"] is True, "the switch is saved from Settings")
notify.enabled = False
vault.enabled = False

# the command line switches on the app's own features, but never for a test's own data files
web.app_mode()
ok(not vault.enabled and not backup.enabled and not notify.enabled and not getattr(logging.getLogger("werkzeug"), "_mm_setup", False),
   "with test data files, app mode leaves Credential Manager, backups, the log and notifications alone")

shutil.rmtree(S, ignore_errors=True)
print("done")
