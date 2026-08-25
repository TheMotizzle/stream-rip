# streamrip

Extract the video stream link (`m3u8` / `mpd` / `mp4` / ...) from a streaming page and play it in VLC.

Give it a page URL — or a raw stream URL — and `streamrip` finds the most likely video source and launches VLC against it. No build step: it's a single self-contained Python script.

## How it works

1. Fetches the page with a browser-like `User-Agent`.
2. Recursively follows nested `<iframe>`s and JavaScript (including iframes built via `document.write`), bounded by `--depth` and `--max-fetches`.
3. Collects every media-source URL it finds in `<video>`/`<source>` tags, raw HTML, and script text.
4. Scores candidates (`m3u8` > `mpd` > common video extensions, plus context keywords like `player`, `source`, `hls`) and picks the best.
5. Fronts the stream with a local reverse proxy on `127.0.0.1` that injects the correct `Referer` and `User-Agent` (stream servers commonly gate on these, and VLC can't set a `Referer` itself), then launches VLC.

## Quick start

```bash
./streamrip.py "https://example.com/some-page"          # find and play the best stream
./streamrip.py "https://example.com/.../index.m3u8"     # play a raw stream URL directly
./streamrip.py "https://example.com/some-page" --no-vlc # print the best URL without VLC
```

If `streamrip.py` isn't executable, run it as `python3 streamrip.py ...`.

## Requirements

- Python 3.8+
- VLC (for playback; not needed with `--no-vlc`, `--all`, `--json`, or `--quiet`)

Python environment setup is automatic:

- If `.venv` exists beside the script, it is used.
- If `requests` or `beautifulsoup4` is missing, `streamrip` offers to create `.venv` and install both there.
- If another virtual environment is active, it offers to install missing dependencies into that environment instead.

Install VLC:

| Platform | Command |
| --- | --- |
| Linux | `sudo apt install vlc` (or `dnf install vlc`, `pacman -S vlc`) |
| macOS | `brew install vlc` |
| Windows | Install the standard build from videolan.org |

## Options

| Flag | Meaning |
| --- | --- |
| `url` | Page (or raw stream) URL — required |
| `--all` | Show every candidate stream URL, not just the best |
| `--chain` | Show the navigation chain that led to the stream |
| `--json` | Machine-readable output |
| `-q`, `--quiet` | Print only the stream URL |
| `--no-vlc` | Print the best stream URL instead of launching VLC |
| `--no-gui` | Use `cvlc` (no video window) |
| `--vlc-bin PATH` | Specific VLC binary (else auto-detected) |
| `--vlc-args "..."` | Extra arguments passed to VLC |
| `--depth N` | Max iframe depth (default `10`) |
| `--max-fetches N` | Max pages/scripts to fetch (default `120`) |
| `--insecure` | Disable TLS certificate verification |
| `--ua STR` | Custom User-Agent string |

Run `streamrip.py --help` for the full guide.

## Examples

```bash
# Print just the stream URL
$ ./streamrip.py "https://example.com/match/123" --no-vlc
https://cdn.example.com/images/foo/index.m3u8

# See every candidate, best first
$ ./streamrip.py "https://example.com/match/123" --all
[ 112] (m3u8) https://cdn.example.com/images/foo/index.m3u8
[  50] (mp4)  https://example.com/trailer.mp4

# See how the stream was found
$ ./streamrip.py "https://example.com/match/123" --chain
Navigation chain:
  0. https://example.com/match/123
  1. https://player.example.com/embed/abc
  2. https://cdn.example.com/images/foo/index.m3u8
```

## Notes

- **Static analysis only.** `streamrip` parses HTML and scans script *text* with regexes — it does not execute JavaScript. Sites that render the player entirely in JS (no media URL or iframe `src` present in the fetched text) will return `No video stream found.`
- **Referer handling.** The proxy uses the player page that embedded the stream as the `Referer`, falling back to the stream's own origin — matching what a real browser sends.
- **Raw stream URLs.** If you pass a bare `.m3u8`/`.mp4`/... URL, it is played directly without crawling.
- **VLC auto-detection order** (when `--vlc-bin` is not given): `vlc`/`cvlc` on PATH → macOS `/Applications/VLC.app/Contents/MacOS/VLC` → Windows `C:\Program Files[ (x86)]\VideoLAN\VLC\vlc.exe` and `%LOCALAPPDATA%\VideoLAN\VLC\vlc.exe`.
- **Live streams** may 404 before or after the event; `streamrip` warns but still launches the player.
