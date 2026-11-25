"""The filter language of the library table (res:4k codec:h264 year:<2000 size:>20gb -genre:horror "some words"),
on the server, so smart collections can be kept up to date without the page open.

It reads the same rows the page gets (library.current_items() for movies, tvlib summaries for shows) and works the
same way as the filter box in static/index.html; tests/test_convert_collections_badges.py checks the examples.
"""
import re
import time
from datetime import datetime

ALIAS = {"hevc": "h265", "x265": "h265", "avc": "h264", "x264": "h264", "xvid": "mpeg4", "divx": "mpeg4", "dv": "dolbyvision",
         "dovi": "dolbyvision", "ddp": "eac3", "dd": "ac3"}
RES = {"4k": 2160, "uhd": 2160, "fhd": 1080, "hd": 720}
FIELD_ALIAS = {"resolution": "res", "genres": "genre", "collection": "set", "age": "mpaa", "rated": "mpaa", "subs": "sub",
               "subtitles": "sub", "language": "lang", "file": "path", "folder": "path", "dups": "dup", "duplicate": "dup",
               "tags": "tag", "eps": "episodes", "length": "runtime", "video": "codec", "channels": "audio"}
TOKEN = re.compile(r'-?\w+:(?:"[^"]*"?|\S*)|-?"[^"]*"?|\S+')


def nz(s) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s if s is not None else "").lower())


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def res_num(v):
    return RES.get(str(v).lower(), num(v))


def size_bytes(v):
    m = re.fullmatch(r"([\d.]+)\s*([tgmk])?i?b?", str(v), re.I)
    if not m:
        return None
    try:
        return float(m.group(1)) * {"t": 1e12, "g": 1e9, "m": 1e6, "k": 1e3}[(m.group(2) or "g").lower()]
    except ValueError:
        return None


def minutes(v):
    m = re.fullmatch(r"(?:(\d+(?:\.\d+)?)\s*h)?\s*(?:(\d+)\s*m?(?:in)?)?", str(v).strip(), re.I)
    if not m or not (m.group(1) or m.group(2)):
        return None
    return float(m.group(1) or 0) * 60 + int(m.group(2) or 0)


def _info(r):
    return r.get("info") or {}


def _ok(m):
    return bool(m) and m.get("status") in ("auto", "manual")


def _year(r):
    return (r.get("match") or {}).get("year") or r.get("year")


def _subs(f):
    return sorted(set(f.get("subs") or []) | set(f.get("subs_in") or []))


def _int(text):
    m = re.match(r"\d+", str(text or ""))
    return int(m.group()) if m else None


MOVIE_FIELDS = {
    "title": ("text", lambda r, c: f"{r.get('title', '')} {(r.get('match') or {}).get('name') or ''}", {}),
    "year": ("num", lambda r, c: _year(r), {}),
    "res": ("num", lambda r, c: _int(r.get("resolution")), {"parse": res_num}),
    "codec": ("text", lambda r, c: r.get("codec"), {}),
    "hdr": ("text", lambda r, c: _info(r).get("hdr"), {}),
    "audio": ("text", lambda r, c: _info(r).get("audio"), {}),
    "lang": ("list", lambda r, c: _info(r).get("audio_langs"), {"eq": True}),
    "sub": ("list", lambda r, c: _subs(r.get("files") or {}), {"eq": True}),
    "size": ("num", lambda r, c: r.get("size"), {"parse": size_bytes, "op": ">="}),
    "runtime": ("num", lambda r, c: _info(r).get("runtime"), {"parse": minutes, "op": ">="}),
    "rating": ("num", lambda r, c: _info(r).get("rating"), {"op": ">="}),
    "bitrate": ("num", lambda r, c: _info(r)["bitrate"] / 1000 if _info(r).get("bitrate") else None, {"op": ">="}),
    "genre": ("list", lambda r, c: _info(r).get("genres"), {}),
    "set": ("text", lambda r, c: _info(r).get("set"), {}),
    "tag": ("list", lambda r, c: _info(r).get("tags"), {}),
    "mpaa": ("text", lambda r, c: _info(r).get("mpaa"), {"eq": True}),
    "added": ("date", lambda r, c: r.get("mtime"), {}),
    "watched": ("bool", lambda r, c: bool(_info(r).get("watched")), {}),
    "named": ("bool", lambda r, c: bool(r.get("named")), {}),
    "matched": ("bool", lambda r, c: _ok(r.get("match")), {}),
    "review": ("bool", lambda r, c: (r.get("match") or {}).get("status") == "review", {}),
    "nfo": ("bool", lambda r, c: bool((r.get("files") or {}).get("nfo")), {}),
    "art": ("bool", lambda r, c: bool((r.get("files") or {}).get("art")), {}),
    "trailer": ("bool", lambda r, c: bool((r.get("files") or {}).get("trailer")), {}),
    "dup": ("bool", lambda r, c: _ok(r.get("match")) and not r.get("part") and c.get(r["match"]["tmdb_id"], 0) > 1, {}),
    "path": ("text", lambda r, c: r.get("path"), {}),
}
TV_FIELDS = {
    "title": MOVIE_FIELDS["title"],
    "year": ("num", lambda r, c: _year(r), {}),
    "res": ("list_num", lambda r, c: [x for x in (_int(k) for k in (r.get("res_counts") or {})) if x], {"parse": res_num}),
    "codec": ("list", lambda r, c: list(r.get("codec_counts") or {}), {}),
    "rating": ("num", lambda r, c: _info(r).get("rating"), {"op": ">="}),
    "genre": ("list", lambda r, c: _info(r).get("genres"), {}),
    "mpaa": ("text", lambda r, c: _info(r).get("mpaa"), {"eq": True}),
    "episodes": ("num", lambda r, c: r.get("episodes"), {"op": ">="}),
    "seasons": ("num", lambda r, c: len([s for s in r.get("seasons") or [] if s > 0]), {}),
    "missing": ("num", lambda r, c: r.get("missing"), {"op": ">="}),
    "size": ("num", lambda r, c: r.get("size"), {"parse": size_bytes, "op": ">="}),
    "sub": ("list", lambda r, c: sorted((r.get("has") or {}).get("subs") or {}), {"eq": True}),
    "added": ("date", lambda r, c: r.get("added"), {}),
    "named": ("bool", lambda r, c: bool(r.get("named")), {}),
    "matched": ("bool", lambda r, c: _ok(r.get("match")), {}),
    "nfo": ("bool", lambda r, c: bool((r.get("has") or {}).get("nfo") and (r.get("has") or {}).get("ep_nfo", 0) >= r.get("files", 0)), {}),
    "art": ("bool", lambda r, c: bool((r.get("has") or {}).get("art")), {}),
    "trailer": ("bool", lambda r, c: bool((r.get("has") or {}).get("trailer")), {}),
    "path": ("text", lambda r, c: r.get("folder"), {}),
}


def parse(q: str, kind: str = "movies") -> dict:
    fields = TV_FIELDS if kind == "tv" else MOVIE_FIELDS
    out = {"terms": [], "words": [], "unknown": [], "kind": kind}
    for tok in TOKEN.findall(q or ""):
        neg = False
        if len(tok) > 1 and tok[0] == "-":
            neg, tok = True, tok[1:]
        m = re.fullmatch(r"(\w+):(.*)", tok, re.S)
        if m:
            name = FIELD_ALIAS.get(m.group(1).lower(), m.group(1).lower())
            if name in fields:
                om = re.fullmatch(r"(>=|<=|!=|>|<|=)?(.*)", m.group(2).strip('"'), re.S)
                op, v = om.group(1) or "", om.group(2)
                if v != "":
                    out["terms"].append({"f": name, "neg": (not neg) if op == "!=" else neg, "op": "" if op == "!=" else op, "v": v})
                continue
            if m.group(2):
                out["unknown"].append(m.group(1))
        w = tok.strip('"').lower()
        if w:
            out["words"].append((neg, w))
    return out


def _hay(r, kind):
    m = (r.get("match") or {}).get("name") or ""
    if kind == "tv":
        return f"{r.get('title', '')} {r.get('folder', '')} {m}".lower()
    return f"{r.get('title', '')} {r.get('year') or ''} {r.get('path', '')} {m}".lower()


def _date_match(t, op, v, now=None):
    if not t:
        return False
    d = re.fullmatch(r"(\d+)\s*([dwmy])", v, re.I)
    if d:
        cut = (now or time.time()) - int(d.group(1)) * {"d": 1, "w": 7, "m": 30.44, "y": 365.25}[d.group(2).lower()] * 86400
        return t < cut if op in (">", ">=") else t >= cut
    m = re.fullmatch(r"(\d{4})(?:-(\d{1,2}))?(?:-(\d{1,2}))?", v)
    if not m:
        return False
    y, mo, da = int(m.group(1)), int(m.group(2)) if m.group(2) else None, int(m.group(3)) if m.group(3) else None
    try:
        start = datetime(y, mo or 1, da or 1).timestamp()
        if da:
            end = start + 86400
        elif mo:
            end = datetime(y + (mo == 12), mo % 12 + 1, 1).timestamp()
        else:
            end = datetime(y + 1, 1, 1).timestamp()
    except ValueError:
        return False
    return {">": t >= end, ">=": t >= start, "<": t < start, "<=": t < end}.get(op, start <= t < end)


def _term(r, t, fields, copies):
    kind, get, opts = fields[t["f"]]
    val, lv, op, v = get(r, copies), t["v"].lower(), t["op"], t["v"]
    if kind == "bool":
        if lv in ("yes", "y", "true", "1", "any"):
            return bool(val)
        if lv in ("no", "n", "false", "0", "none"):
            return not val
        return False
    if kind in ("text", "list"):
        arr = [x for x in ((val or []) if kind == "list" else ([val] if val else [])) if x]
        special = {"any": 1, "none": 0} if opts.get("eq") else {"any": 1, "yes": 1, "none": 0, "no": 0}
        if lv in special:
            return bool(arr) if special[lv] else not arr
        want = ALIAS.get(nz(v), nz(v))
        return any(nz(x) == want if opts.get("eq") else want in nz(x) for x in arr)
    if kind in ("num", "list_num"):
        xs = [x for x in (val if isinstance(val, list) else [val]) if isinstance(x, (int, float))]
        if lv in ("any", "yes"):
            return any(x > 0 for x in xs)
        if lv in ("none", "no"):
            return not any(x > 0 for x in xs)
        p = opts.get("parse") or num
        if lv == "sd" and p is res_num and not op:
            return any(x < 720 for x in xs)
        rg = re.fullmatch(r"([^-.]+?)(?:-|\.\.)(.+)", v)
        if rg and not op:
            a, b = p(rg.group(1)), p(rg.group(2))
            if a is not None and b is not None:
                return any(min(a, b) <= x <= max(a, b) for x in xs)
        n = p(v)
        if n is None:
            return False
        o = op or opts.get("op") or "="
        return any({">": x > n, ">=": x >= n, "<": x < n, "<=": x <= n}.get(o, abs(x - n) < 1e-9) for x in xs)
    if kind == "date":
        return _date_match(val, op, v)
    return True


def copies_of(rows: list) -> dict:
    out = {}
    for r in rows:
        m = r.get("match")
        if _ok(m) and not r.get("part"):
            out[m["tmdb_id"]] = out.get(m["tmdb_id"], 0) + 1
    return out


def matches(r: dict, qs: dict, copies: dict = None) -> bool:
    fields = TV_FIELDS if qs["kind"] == "tv" else MOVIE_FIELDS
    if qs["words"]:
        h = _hay(r, qs["kind"])
        if not all((w in h) != neg for neg, w in qs["words"]):
            return False
    return all(_term(r, t, fields, copies or {}) != t["neg"] for t in qs["terms"])


def select(rows: list, q: str, kind: str = "movies") -> list:
    qs = parse(q, kind)
    copies = copies_of(rows) if kind != "tv" else {}
    return [r for r in rows if matches(r, qs, copies)]
