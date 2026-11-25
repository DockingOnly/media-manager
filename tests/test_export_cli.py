"""Export (CSV / HTML) and command-line mode."""
import contextlib
import io
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mm import cli, core, tmdbapi, web  # noqa: E402

S = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="mm-test-"))
lib, st = S / "libE", S / "stE"
for d in (lib, st):
    shutil.rmtree(d, ignore_errors=True)
st.mkdir(parents=True)
for name in ("CONFIG", "MATCHES", "RENAME_LOG", "LIBRARY", "EDITS", "HISTORY", "PROBES"):
    setattr(core, name, st / f"{name.lower()}.json")
core.save_json(core.CONFIG, {"tmdb_key": "x", "folder": str(lib)})
ok = lambda cond, msg: print(("PASS " if cond else "FAIL ") + msg)
(lib / "Heat (1995)").mkdir(parents=True)
(lib / "Heat (1995)/Heat (1995).mkv").write_bytes(b"x")
(lib / "alien.1979.1080p.mkv").write_bytes(b"x")
tmdbapi.tmdb = lambda path, **k: {}


def cli_run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.run(list(argv))
    return code, out.getvalue(), err.getvalue()


code, out, _ = cli_run("movies", str(lib), "scan", "--quiet")
ok(code == 0 and "2 video files" in out, f"CLI scans the folder: {out.strip()}")
items = core.state["items"]
core.save_json(core.MATCHES, {i["id"]: {"tmdb_id": 949 if "Heat" in i["path"] else 348, "status": "auto", "title": "Heat" if "Heat" in i["path"] else "Alien",
                                        "year": 1995 if "Heat" in i["path"] else 1979} for i in items})

c = web.app.test_client()
r = c.get("/api/export", query_string={"kind": "movies", "format": "csv"})
text = r.data.decode("utf-8-sig")
ok(r.status_code == 200 and 'filename="movies-' in r.headers["Content-Disposition"] and text.splitlines()[0].startswith("Title,Year,TMDB"),
   "movies export downloads as a CSV with a header row")
ok("Alien,1979,348" in text and "Heat,1995,949" in text and r.data.startswith(b"\xef\xbb\xbf"), "one row per movie (with the BOM Excel needs)")
r = c.get("/api/export", query_string={"kind": "movies", "format": "html"})
ok(b"<table>" in r.data and b"Alien" in r.data and b'id="q"' in r.data, "the HTML export is a page with a filterable table")
r = c.get("/api/export", query_string={"kind": "collections", "format": "csv"})
ok(r.status_code == 400 and "Check collections first" in r.get_json()["error"], "collections export asks you to check collections first")
r = c.get("/api/export", query_string={"kind": "missing", "format": "csv"})
ok(r.status_code == 400 and "TV" in r.get_json()["error"], "missing-episode export needs the TV folder scanned")

code, out, _ = cli_run("movies", str(lib), "rename")
ok(code == 0 and "would be renamed" in out and "Alien (1979)" in out and (lib / "alien.1979.1080p.mkv").exists(),
   "CLI rename without --apply only lists the change")
code, out, _ = cli_run("movies", str(lib), "rename", "--apply", "--quiet")
ok(code == 0 and (lib / "Alien (1979)/Alien (1979).mkv").exists(), "with --apply it renames")
target = S / "out.csv"
code, out, _ = cli_run("movies", str(lib), "export", str(target), "--quiet")
ok(code == 0 and target.read_text(encoding="utf-8-sig").count("\n") == 3, "CLI export writes the file")
code, _, err = cli_run("movies", str(lib), "export")
ok(code == 1 and "needs a file name" in err, "export without a file name explains itself")
code, _, err = cli_run("movies", str(lib), "dance")
ok(code == 1 and "Unknown step" in err, "an unknown step is an error, with the list of steps")
code, _, err = cli_run("movies", str(S / "nope"), "scan")
ok(code == 1 and "Folder not found" in err, "a missing folder is an error")
shutil.rmtree(S, ignore_errors=True)
print("done")
