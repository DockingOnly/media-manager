"""Command-line mode: run the same tasks as the browser page, for scripts and Task Scheduler.

    .venv\\Scripts\\python app.py --help
    .venv\\Scripts\\python app.py movies D:\\Media\\Movies scan match nfo
    .venv\\Scripts\\python app.py tv D:\\Media\\Shows scan match rename --apply
    .venv\\Scripts\\python app.py movies D:\\Media\\Movies export movies.csv
    .venv\\Scripts\\python app.py watch      (watch for new files, as set in Settings, until you close it)

It runs the app's own code in this process (no browser, no server), with the same settings and saved data.
Renaming only shows what would change unless you add --apply.
"""
import argparse
import sys
import time
from pathlib import Path

STEPS = ["scan", "match", "nfo", "artwork", "trailers", "subtitles", "rename", "collections", "trakt", "export"]
ART = {"movies": ["poster", "fanart"], "tv": ["poster", "fanart", "season", "thumb"]}  # the usual set; the page offers more


class Failed(Exception):
    pass


def parser():
    p = argparse.ArgumentParser(prog="app.py", description="Media manager, command-line mode. With no arguments, app.py opens the browser page.",
                                epilog="Steps run in the order given. Example: app.py movies D:\\Media\\Movies scan match nfo rename --apply")
    p.add_argument("library", choices=["movies", "tv"], help="which kind of library")
    p.add_argument("folder", help="the library folder")
    p.add_argument("steps", nargs="+", metavar="step", help="one or more of: " + ", ".join(STEPS) + " (export takes a file name: export movies.csv)")
    p.add_argument("--apply", action="store_true", help="actually rename (without it, rename only lists what would change)")
    p.add_argument("--overwrite", action="store_true", help="nfo: rebuild .nfo files that already exist (your edits and watched status are kept)")
    p.add_argument("--replace", action="store_true", help="artwork: replace images that already exist")
    p.add_argument("--redo", action="store_true", help="match: match again, except titles you matched by hand")
    p.add_argument("--clear-unwatched", action="store_true", help="trakt: also mark titles unwatched when Trakt says so")
    p.add_argument("--languages", default="", help="subtitles: languages, e.g. en,fr (default: your Settings)")
    p.add_argument("--quiet", action="store_true", help="only print errors and the final summaries")
    return p


def run(argv=None) -> int:
    args = parser().parse_args(argv)
    from . import web  # imports the app (routes and all) without starting a server
    c = web.app.test_client()
    tv = args.library == "tv"
    pre = "/api/tv" if tv else "/api"
    say = (lambda *a: None) if args.quiet else print

    def call(method, url, data=None):
        r = c.post(url, json=data or {}) if method == "post" else c.get(url, query_string=data)
        d = r.get_json(silent=True)
        if r.status_code >= 400:
            raise Failed((d or {}).get("error") or f"{url} failed ({r.status_code})")
        return r, d

    def job(url, data=None):
        _, d = call("post", url, data)
        last = ""
        while True:
            j = c.get(f"/api/job/{d['job']}").get_json()
            line = f"  {j['label']}" + (f" {j['done']}/{j['total']}" if j["total"] else "") + (f" - {j['detail']}" if j.get("detail") else "")
            if line != last and not args.quiet:
                print(line[:150].ljust(min(len(last), 150)), end="\r", flush=True)
                last = line
            if j["finished"]:
                if last and not args.quiet:
                    print(" " * min(len(last), 150), end="\r")
                if j["error"]:
                    raise Failed(j["error"])
                return j["result"]
            time.sleep(0.3)

    steps = list(args.steps)
    try:
        while steps:
            step = steps.pop(0)
            if step not in STEPS:
                raise Failed(f"Unknown step '{step}'. Steps: {', '.join(STEPS)}")
            if step != "scan" and step != "export" and not _scanned(tv):
                steps.insert(0, step)
                step = "scan"
            say(f"> {step}")
            if step == "scan":
                folder = Path(args.folder)
                r = job(f"{pre}/scan", {"folder": str(folder)})
                n = len(r.get("shows") or []) if tv else len(r["items"])
                print(f"Scanned {folder}: {n} {'shows' if tv else 'video files'}." + _changes(r.get("changes")))
            elif step == "match":
                r = job(f"{pre}/match-all", {"redo": args.redo})
                print("Matched." if tv else f"Matched: {_summary(r)}")
            elif step == "nfo":
                r = job(f"{pre}/nfo", {"overwrite": args.overwrite, "artwork": False})
                n = r.get("show_nfos", 0) + r.get("seasons", 0) + r.get("episodes", 0) if tv else r.get("nfo", 0)
                print(f"Wrote {n} .nfo files" + (f", {len(r['failed'])} failed" if r.get("failed") else "") + "."
                      + (f" Ratings left out: {r['ratings_note']}" if r.get("ratings_note") else ""))
                _failures(r)
            elif step == "artwork":
                r = job(f"{pre}/artwork", {"kinds": ART[args.library], "replace": args.replace})
                print(f"Artwork: {r.get('downloaded', 0)} images saved, {r.get('had', 0)} already there." + (f" {len(r['failed'])} failed." if r.get("failed") else ""))
                _failures(r)
            elif step == "trailers":
                r = job(f"{pre}/trailers", {})
                print(f"Trailers: {r.get('downloaded', 0)} saved, {r.get('had', 0)} already there." + (f" {len(r['failed'])} failed." if r.get("failed") else ""))
                _failures(r)
            elif step == "subtitles":
                data = {"languages": args.languages} if args.languages else {}
                r = job(f"{pre}/subtitles", data)
                print(f"Subtitles: {r.get('downloaded', 0)} saved, {r.get('had', 0)} already there." + (f" {len(r['failed'])} failed." if r.get("failed") else ""))
                _failures(r)
            elif step == "rename":
                _, pv = call("get", f"{pre}/rename/preview")
                plans = [p for p in pv["plans"] if not p.get("reason")]
                for p in plans:
                    print(f"  {p['name']}")
                    for ch in p.get("changes", []):
                        print(f"     {ch['from']}\n  -> {ch['to']}")
                held = [p for p in pv["plans"] if p.get("reason")]
                for p in held:
                    print(f"  (left alone) {p['name']}: {p['reason']}")
                if not plans:
                    print("Rename: everything is already named for Jellyfin.")
                elif not args.apply:
                    print(f"Rename: {len(plans)} would be renamed. Add --apply to rename them.")
                else:
                    r = job(f"{pre}/rename/apply", {"ids": [p["id"] for p in plans]})
                    print(f"Renamed {r.get('renamed', len(plans))}. You can undo this from the browser page.")
                    _failures(r)
            elif step == "collections":
                if tv:
                    raise Failed("Collections are for movies.")
                r = job("/api/collections", {})
                for col in r["collections"]:
                    gaps = [m for m in col["missing"] if m.get("released")]
                    print(f"  {col['name']}: have {len(col['have'])}" + (f", missing {', '.join(m['title'] for m in gaps)}" if gaps else ""))
                print(f"Collections: {len(r['collections'])}.")
            elif step == "trakt":
                r = job("/api/trakt/sync", {"kind": args.library, "clear": args.clear_unwatched})
                print(f"Trakt: {r['updated']} .nfo files updated; {r['watched']} watched on Trakt.")
            elif step == "export":
                if not steps or steps[0] in STEPS:
                    raise Failed("export needs a file name ending in .csv or .html, e.g. export movies.csv or export missing.html")
                target = Path(steps.pop(0))
                kind = next((k for k in ("missing", "collections", "tv", "movies") if target.stem.lower().startswith(k)), "tv" if tv else "movies")
                fmt = "html" if target.suffix.lower() in (".html", ".htm") else "csv"
                r, _ = call("get", "/api/export", {"kind": kind, "format": fmt})
                target.write_bytes(r.data)
                print(f"Exported {kind} to {target.resolve()}")
    except Failed as err:
        print(f"Error: {err}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        return 130
    return 0


def watch_forever() -> int:
    """app.py watch: look for new files every few minutes (Settings: how often, and which steps), and tell Jellyfin."""
    from . import core, jellyfin, watch, web
    if web.port_in_use(core.PORT):
        print("The media manager is already running; it watches for new files itself while watching is on in Settings.")
        return 1
    s = watch.settings()
    if not s["on"]:
        print("Watching is off. Turn it on in Settings (Watch for new files) first.")
        return 1
    print(f"Watching your library folders every {s['minutes']} minutes ({', '.join(['scan'] + s['steps'])}). Close this window to stop.")
    jellyfin.start()
    say = lambda text: print(time.strftime("%H:%M ") + text, flush=True)
    try:
        watch.loop(say)
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


def _scanned(tv: bool) -> bool:
    from . import core
    return (core.tv_state if tv else core.state)["root"] is not None


def _changes(ch) -> str:
    return f" ({ch['added']} new, {ch['removed']} gone since last time)" if ch and (ch["added"] or ch["removed"]) else ""


def _summary(r) -> str:
    from . import core
    items = r.get("items") or []
    matched = sum(1 for i in items if (i.get("match") or {}).get("status") in core.MATCHED)
    return f"{matched} of {len(items)} files matched to TMDB."


def _failures(r) -> None:
    for f in (r or {}).get("failed") or []:
        print(f"  failed: {f if isinstance(f, str) else f.get('name') or f.get('path') or f}: {'' if isinstance(f, str) else f.get('error', '')}")
