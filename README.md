# Media manager

A local web app that gets movie and TV libraries ready for **Jellyfin**: it matches files on TMDB, renames them the
way Jellyfin expects, writes `.nfo` files, and downloads artwork, trailers and subtitles. It is built as a safer,
Jellyfin-first alternative to tinyMediaManager.

Everything runs on your own computer at `http://127.0.0.1:8765`. Your files, keys and library data stay there.

## Starting it

Double-click **`run.bat`**. The first run sets up Python libraries (Python 3.9 or newer must be installed). The
page opens in your browser; closing the tab stops the app, unless watching for new files is set to keep running.

Open **Settings** (bottom of the sidebar) and paste a free TMDB API key (themoviedb.org → Settings → API).
Everything else is optional.

Or run the packaged **`MediaManager.exe`** (see [Test builds](#test-builds)): no Python needed.

## Two views

The same library, two ways to look at it. Switch at the top of the page or press **V**; the filter, the selection
and the highlighted item carry over.

* **Technical** — the table, with an icon toolbar for every task. For finding and fixing.
* **Theatrical** — posters (a wall, or shelves by collection) with flags for anything to fix: SD, more than one
  copy, no trailer, check the match, rename. Click a flag to see those in Technical.

Beside both: a sidebar (smart collections, collections, and what needs attention, with counts) and, for the
highlighted movie or show, its artwork, what it has, and Play / Details / everything else.

## What it does

| | |
|---|---|
| **Match** | Movies and shows on TMDB, using IDs already in `.nfo` files first. Episode orders (aired, DVD, absolute) per show or per season. |
| **Rename** | `Title (Year)/Title (Year).mkv`, `Show (Year)/Season 1/Show - S01E02 - Title.mkv`, or your own patterns. Always a preview first, then undo. Version labels (`- Theatrical`) are kept; movies whose files sit in different folders are held back for you to check. |
| **DVD extras** | Extras numbered as episodes go to TMDB's specials (season 0) when their titles match, otherwise to Jellyfin's extras folders. |
| **.nfo files** | Kodi/Jellyfin `.nfo` with technical details (codecs, HDR, audio, languages), IMDb / Rotten Tomatoes / Metacritic ratings (OMDb key), and your own edits, which survive rewrites. |
| **Artwork** | Posters, backdrops, logos, thumbnails, banners, clear art, disc art, season posters and episode thumbnails (TMDB, plus fanart.tv with a key). |
| **Trailers and subtitles** | Trailers from TMDB/YouTube; subtitles from OpenSubtitles.com, skipping languages already inside the video. |
| **Trakt** | Watched status from Trakt into the `.nfo` files. |
| **Collections** | Which films of a series you have and which are missing. |
| **Jellyfin** | Tells Jellyfin which folders changed after every task, so it refreshes just those. Checks Jellyfin's library settings for anything that fights the app's files, with one-click fixes. |
| **Watch for new files** | Every few minutes, new files that have finished copying are matched and get `.nfo` files and artwork. Renames always wait for you. |
| **Tools** | Playback check (which files Jellyfin will have to convert, per kind of player, and which can't be read), duplicates (copies compared side by side), leftovers (folders with nothing left to play). |
| **History** | Everything the app moved, recycled or wrote, task by task. **Restore** brings things back from the Recycle Bin; **Move back** undoes a task's moves when every file is still where the app put it. |
| **Export** | Movies, episodes, missing episodes and collections as CSV or a web page. |
| **Filter and columns** | Type fields into the filter box: `res:4k codec:h264 year:<2000 size:>20gb sub:none -genre:horror` (the **?** beside it lists them all). Choose columns with **Columns**: runtime, rating, HDR, audio, bitrate, genres, collection, age rating, date added. |
| **Keyboard and right-click** | `J`/`K` or the arrows move through rows, `Space` selects, shift-click selects a range, `Enter` opens, `M` matches, `P` plays, `F` opens the folder, `Delete` deletes (to the Recycle Bin, after asking), `V` switches views, `?` lists the rest. Right-click a row or poster for everything, including **Show in Jellyfin**. |
| **Library stats** | Size by resolution and format, audio, HDR, decades, genres, the biggest files, and how much space removing duplicates or converting older formats would free. |
| **Backups** | Your matches, edits, rename undo, History and settings are copied when the app starts and daily (newest 10 kept), with **Restore** under Tools → Backups. |
| **Notifications** | A Windows notification when the folder watcher has handled new files. |
| **Convert to a smaller format** | Older files (H.264, MPEG-4, MPEG-2, VC-1…) to H.265 or AV1, on the graphics card when it can, keeping every audio and subtitle track. Only files with bitrate to spare are offered, each copy is checked (length, tracks, and a VMAF quality score on samples) before it replaces the original, and the original goes to the Recycle Bin. Runs in the background, keeps the PC awake, and keeps going after the page is closed. |
| **Smart collections** | Jellyfin collections that follow a rule written like the filter box (`res:4k hdr:yes`, `rating:8 genre:crime`), optionally limited to a TMDB list. Kept up to date as new files arrive, each with a poster made from its first four. |
| **Poster badges** | 4K, HDR / Dolby Vision, Atmos / DTS:X / 7.1 and the rating on movie posters. The originals are kept, and **Remove all badges** puts them back exactly. |

## Safety

* Nothing is renamed, moved or deleted without a preview and your choice.
* Deleting means the **Recycle Bin**, and the app checks first that Windows will really keep it there (paths too
  long or items too big for the bin would otherwise be deleted for good).
* The last copy of a movie or episode is never recycled by the duplicates tool.
* Every change is listed under Tools → History, with Restore and Move back.
* A converted video replaces its original only after the copy has been checked; Restore in History brings the
  original back (and sends the converted copy to the Recycle Bin).
* API keys and passwords are kept in **Windows Credential Manager** (encrypted with your Windows login), not in
  `config.json`; the app moves them there when it starts. They're listed there as `media-manager/...`.
* The app's window shows only actions and problems; `logs\media-manager.log` keeps the details.

## Test builds

`packaging\build.bat` builds **`dist\MediaManager\MediaManager.exe`** (Python, ffmpeg and the trailer helper
included) and a zip of it to send to testers, with `packaging\TESTERS.txt` as its read-me. Only the program goes
in: settings, keys and library data are never bundled.

The packaged app keeps its data in `%LOCALAPPDATA%\Media Manager` instead of its own folder, so unzipping a newer
build over an older one keeps everything. Keys stay in Credential Manager, shared with a copy run from source on
the same PC.

To try a build without touching an existing setup, set these before starting it:

| | |
|---|---|
| `MM_DATA` | a folder for its data (for example an empty one) |
| `MM_PORT` | another port, to run next to a copy that's already open |
| `MM_ISOLATED=1` | no Credential Manager, backups, log file or notifications |
| `MM_NO_BROWSER=1` | don't open a browser tab |

YouTube changes often: if trailer downloads stop working in a build, rebuild it (the build takes the current
yt-dlp), or use `update-downloader.bat` when running from source.

## Command line

```
.venv\Scripts\python app.py --help
.venv\Scripts\python app.py movies D:\Media\Movies scan match nfo artwork
.venv\Scripts\python app.py tv D:\Media\Shows scan match rename --apply
.venv\Scripts\python app.py watch
```

## Tests

```
.venv\Scripts\python tests\run_all.py
```
