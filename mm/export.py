"""Export your library as a spreadsheet (CSV, opens in Excel) or a web page (HTML): movies, TV episodes,
missing episodes, and collections."""
import csv
import datetime
import html
import io

from . import core, library, tvlib, tvmeta

KINDS = {"movies": "Movies", "tv": "TV episodes", "missing": "Missing episodes", "collections": "Collections"}


def gb(size) -> str:
    return f"{(size or 0) / 1024 ** 3:.2f}"


def yes(v) -> str:
    return "yes" if v else ""


def movie_rows(items: list):
    head = ["Title", "Year", "TMDB", "Resolution", "Codec", "Size (GB)", "Named for Jellyfin", ".nfo", "Artwork", "Trailer", "Subtitles", "File"]
    rows = []
    for it in sorted(items, key=lambda i: ((i.get("match") or {}).get("title") or i.get("title") or "").lower()):
        m = it.get("match") or {}
        matched = m.get("status") in core.MATCHED
        f = it.get("files") or {}
        rows.append([m.get("title") if matched else it.get("title", ""), (m.get("year") if matched else it.get("year")) or "",
                     m.get("tmdb_id", "") if matched else "", it.get("resolution", ""), it.get("codec", ""), gb(it.get("size")),
                     yes(it.get("named")), yes(f.get("nfo")), yes(f.get("art")), yes(f.get("trailer")), " ".join(f.get("subs") or []), it["path"]])
    return head, rows


def tv_rows(items: list):
    head = ["Show", "Season", "Episode", "Title", "Resolution", "Size (GB)", "Named for Jellyfin", ".nfo", "Thumbnail", "Subtitles", "File"]
    rows = []
    for it in sorted((i for i in items if i["kind"] == "episode"), key=lambda i: (i.get("show_dir") or "", i["season"] or 0, i["episodes"])):
        f = it.get("files") or {}
        eps = "-".join(f"{n:02d}" for n in it["episodes"]) if it["episodes"] else ""
        rows.append([it.get("show_dir") or it.get("series", ""), it["season"] if it["season"] is not None else "", eps, it.get("ep_title", ""),
                     it.get("resolution", ""), gb(it.get("size")), yes(it.get("named")), yes(f.get("nfo")), yes(f.get("thumb")),
                     " ".join(f.get("subs") or []), it["path"]])
    return head, rows


def missing_rows(items: list):
    head = ["Show", "Season", "Episode", "Title", "First aired"]
    rows = []
    for key, eps in sorted(tvlib.group(items).items(), key=lambda kv: (kv[1][0].get("show_dir") or "").lower()):
        m = eps[0].get("match")
        if not tvlib.confirmed(m):
            continue
        have = {(e["season"], n) for e in eps if e["kind"] == "episode" for n in e["episodes"]}
        for x in tvmeta.missing_episodes(tvmeta.cached_for(m), have):
            rows.append([eps[0].get("show_dir") or m.get("name", ""), x["season"], x["episode"], x["name"], x["air_date"]])
    return head, rows


def collection_rows(result: dict):
    head = ["Collection", "Title", "Year", "In your library", "Released"]
    rows = []
    for col in (result or {}).get("collections", []):
        for h in col.get("have", []):
            rows.append([col["name"], h.get("title", ""), h.get("year") or "", "yes", "yes"])
        for x in col.get("missing", []):
            rows.append([col["name"], x.get("title", ""), x.get("year") or "", "", yes(x.get("released"))])
    return head, rows


def rows_for(kind: str, collections_result: dict = None):
    if kind == "movies":
        core.require_scan()
        with core.lock:
            return movie_rows(library.current_items())
    if kind in ("tv", "missing"):
        root = core.require_tv_scan()
        with core.lock:
            items = tvlib.annotate(tvlib.with_matches(core.tv_state["items"], root), root)
        return tv_rows(items) if kind == "tv" else missing_rows(items)
    if kind == "collections":
        if not collections_result:
            raise core.UserError("Check collections first (the Collections button), then export them.")
        return collection_rows(collections_result)
    raise core.UserError("Choose what to export: movies, tv, missing or collections.")


def to_csv(head, rows) -> bytes:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(head)
    w.writerows(rows)
    return out.getvalue().encode("utf-8-sig")  # the BOM makes Excel read accents correctly


def to_html(title: str, head, rows) -> bytes:
    e = html.escape
    when = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    body = "".join("<tr>" + "".join(f"<td>{e(str(c))}</td>" for c in r) + "</tr>\n" for r in rows)
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{e(title)}</title>
<style>body{{font:14px system-ui,sans-serif;margin:24px;color:#222}}table{{border-collapse:collapse;width:100%}}
th,td{{border-bottom:1px solid #ddd;padding:5px 8px;text-align:left;vertical-align:top}}th{{background:#f3f3f3;position:sticky;top:0;cursor:pointer}}
tr:hover td{{background:#fafafa}}input{{padding:6px;width:300px;margin-bottom:10px}}</style></head><body>
<h1>{e(title)}</h1><p>{len(rows)} rows, exported {e(when)}.</p>
<input id="q" type="search" placeholder="Filter…" aria-label="Filter rows">
<table><thead><tr>{"".join(f"<th>{e(h)}</th>" for h in head)}</tr></thead><tbody>
{body}</tbody></table>
<script>
const q=document.getElementById("q"),rows=[...document.querySelectorAll("tbody tr")];
q.oninput=()=>{{const t=q.value.toLowerCase();rows.forEach(r=>r.hidden=t&&!r.textContent.toLowerCase().includes(t))}};
document.querySelectorAll("th").forEach((th,i)=>th.onclick=()=>{{const tb=document.querySelector("tbody"),asc=th.dataset.asc!=="1";th.dataset.asc=asc?"1":"0";
  rows.sort((a,b)=>{{const x=a.cells[i].textContent,y=b.cells[i].textContent,n=parseFloat(x)-parseFloat(y);return (isNaN(n)?x.localeCompare(y):n)*(asc?1:-1)}}).forEach(r=>tb.appendChild(r))}});
</script></body></html>"""
    return page.encode("utf-8")


def export(kind: str, fmt: str, collections_result: dict = None):
    """Returns (file name, bytes, content type)."""
    head, rows = rows_for(kind, collections_result)
    stamp = datetime.date.today().isoformat()
    name = f"{kind}-{stamp}"
    if fmt == "html":
        return name + ".html", to_html(KINDS[kind], head, rows), "text/html; charset=utf-8"
    return name + ".csv", to_csv(head, rows), "text/csv; charset=utf-8"
