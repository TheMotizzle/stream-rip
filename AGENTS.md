# streamrip

Single-file Python tool: extracts the video stream URL (`m3u8`/`mpd`/`mp4`/...) from a streaming page and plays it in VLC by default.

- **File:** `streamrip.py` (self-contained, no build step)
- **Repo:** https://github.com/TheMotizzle/stream-rip (branch `main`)
- **Local path:** `/Users/matt/projects/stream-rip`

## Quick start

```bash
./streamrip.py "<page-url>"                # find and play the best stream
./streamrip.py "<m3u8-url>"                # play a raw stream URL directly
./streamrip.py "<page-url>" --no-vlc       # print the best URL without VLC
```

Python setup is automatic. If `.venv` exists beside the script, `streamrip.py` re-executes itself with that environment. If `requests` or `beautifulsoup4` is missing, it prompts before creating `.venv` (recommended) and installing both packages. An already-active virtual environment takes precedence, and missing packages are offered for installation there. On Apple Python builds that use LibreSSL, bootstrap installs `urllib3<2` to avoid urllib3 2.x compatibility warnings.

VLC is the default behavior. Use `--no-vlc`, `--all`, `--json`, or `--quiet` for reporting without playback. The old `--vlc` flag remains accepted as a hidden backward-compatible no-op. Full install/usage guidance is in `--help`.

## How it works

1. **Bootstrap** — Before importing third-party packages, detects the active/project virtual environment and prompts to install anything missing. It cannot activate a venv in the parent shell, so it re-executes the current command with the venv's Python instead.
2. **Crawl** — BFS over a queue of `[url, referer, base, kind, depth]` where `kind` is `doc` or `script`. Follows `<iframe src>` and `<script src>`; inline `<script>` text is scanned in place. Depth (`--depth`) and a fetch cap (`--max-fetches`) bound the crawl.
3. **Collect** — Media URLs are matched by extension (`MEDIA_EXT_RE`: `m3u8, mpd, mp4, m4v, mkv, webm, mov, avi, flv, ts, ogv, ogg`) found in (a) `<video>/<source src>`, (b) quoted URLs in raw HTML, and (c) quoted / `src:` URLs inside script text. Non-media `src:` URLs in scripts are treated as iframes built via `document.write` and enqueued as new docs owned by the enclosing page.
4. **Score & pick best** — `m3u8`=100, `mpd`=90, common video exts=50, `ts`=10; **+8 per context keyword** (`source`, `player`, `file`, `hls`, `m3u8`, `stream`, `clappr`). `best()` returns the highest-scoring entry.
5. **Play (default)** — Fronts the stream with a local reverse proxy (`LocalProxy` on `127.0.0.1:<port>`) that injects the required `Referer` + `User-Agent`, then launches VLC against the local URL.

## Why it's built this way (non-obvious gotchas)

- **Streaming servers gate on `Referer`.** A real browser sends the embedding player page as `Referer`. `streamrip` carries the correct referer through the crawl and into playback. `play_with_vlc` uses the player page (`best["pages"][0]`) as the referer, falling back to the stream's own origin.
- **VLC cannot set an HTTP `Referer`.** Verified on VLC 3.0.20: no global `--http-referer`, MRL `:option=` values are not honored, and the `vlcrc [http]` input section has no referer key. The local proxy is the only reliable way to inject the header for the player — don't remove it thinking VLC options would work.
- **It does NOT execute JavaScript.** It parses static HTML and scans inline/external script *text* with regexes. Sites that fully JS-render the player (no iframe `src` or media URL present in the fetched text) will return "No video stream found."
- **Traversal model:** `doc` = a page/iframe (its children are owned by it); `script` = a JS file (its `base` is the owning document, so iframes it writes get `Referer` = that document). This is what makes cross-origin iframe chains (e.g. a page on one host embedding a player on another) resolve correctly.
- **Raw media URL input** (a bare `.m3u8`, etc.) is recorded directly with no crawl.

## CLI reference

| Flag | Meaning |
| --- | --- |
| `url` | Page (or raw stream) URL — required |
| `--all` | Show every candidate stream URL, not just the best |
| `--chain` | Show the navigation chain that led to the stream |
| `--json` | Machine-readable output |
| `-q` / `--quiet` | Print only the stream URL |
| `--no-vlc` | Print the best stream URL instead of launching VLC |
| `--no-gui` | Use `cvlc` (no window) |
| `--vlc-bin PATH` | Specific VLC binary (else auto-detected) |
| `--vlc-args "..."` | Extra arguments passed to VLC |
| `--depth N` | Max iframe depth (default 10) |
| `--max-fetches N` | Max pages/scripts to fetch (default 120) |
| `--insecure` | Disable TLS certificate verification |
| `--ua STR` | Custom User-Agent string |

## Platforms

- Python 3.8+; cross-platform (Linux / macOS / Windows); no build step.
- **VLC auto-detection order** (used when `--vlc-bin` is not given): `vlc`/`cvlc` on PATH → macOS `/Applications/VLC.app/Contents/MacOS/VLC` → Windows `C:\Program Files[ (x86)]\VideoLAN\VLC\vlc.exe` and `%LOCALAPPDATA%\VideoLAN\VLC\vlc.exe` (appends `.exe`).
- Windows console encoding is guarded (stdout/stderr → UTF-8, `errors=replace`) so a non-ASCII URL never crashes the run.

## Test / verify

Known-good example (a live-game page; the exact URL and stream may expire over time, and DNS in some sandboxes is flaky — retry on `NameResolutionError`):

```bash
python3 streamrip.py "https://vipboxi.net/football/notch-248aa1-denver-broncos-green-bay-packers?l=2891632059" --no-vlc
# -> https://www.cdn291.info/images/dunga43/index.m3u8
```

Navigation chain: `vipboxi.net` → `dungatv.xyz/dunga43.php` → `aquaaqua.top/n1.php?hash=dunga43` → `cdn291.info/page.php` (Clappr player) → `.m3u8`.

Verify playback headlessly:

```bash
python3 streamrip.py "https://www.cdn291.info/images/dunga43/index.m3u8" --no-gui
```

## Notes for editing

- Keep it a **single self-contained file** using only the stdlib + `requests` + `beautifulsoup4`.
- Keep environment bootstrapping above the third-party imports so a fresh machine can reach the install prompt.
- Detection and scoring are **heuristic** — prefer improving `score()` / `MEDIA_EXT_RE` / the regexes over adding per-site special cases.
- The `--help` epilog (`USAGE_GUIDE`) documents install/usage — keep it in sync if flags or platforms change.
