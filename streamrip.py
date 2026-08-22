#!/usr/bin/env python3
"""
streamrip - extract the video stream link (m3u8/mp4/mpd/...) from a streaming page.

Fetches a page, then recursively follows nested <iframe>s and JavaScript
(including scripts that build iframes via document.write), collecting every
media-source URL it can find. By default it prints the single most-likely
stream URL; use --all / --chain / --json for more detail.

Usage:
    python3 streamrip.py <url>
    python3 streamrip.py <url> --all
    python3 streamrip.py <url> --json
"""

import argparse
import http.server
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Cross-platform install / usage guide shown by --help.
USAGE_GUIDE = """
HOW TO RUN / INSTALL (all platforms)
  Requirements: Python 3.8+, the `requests` and `beautifulsoup4` packages.
  VLC is only needed when using --vlc.

  1) Install the Python dependencies (any platform):
         pip install requests beautifulsoup4
     (if pip is missing or protected, try:  pip3 install --user ...)

  2) Install VLC (only for --vlc):
         Linux   :  sudo apt install vlc      (or: dnf install / pacman -S vlc)
         macOS   :  brew install vlc          (or download from videolan.org)
         Windows :  Install the standard build from videolan.org

  3) Run it:
         streamrip.py <page-url>               print the best stream URL
         streamrip.py <page-url>  --vlc  play the stream in VLC
         streamrip.py <m3u8-url>  --vlc  play a raw stream URL directly
     (If `streamrip.py` isn't executable, run it as:  python streamrip.py ...)

  Useful options:
         --all      show every candidate stream URL, not just the best
         --chain    show the navigation chain that led to the stream
         --json     machine-readable output
         --no-gui   with --vlc, use cvlc (no video window)
         --vlc-bin  point at a VLC binary if auto-detection misses it

  VLC auto-detection order (used when --vlc-bin is not given):
         1. vlc / cvlc found on PATH
         2. macOS   : /Applications/VLC.app/Contents/MacOS/VLC
         3. Windows : C:\\Program Files[ (x86)]\\VideoLAN\\VLC\\vlc.exe
                      %LOCALAPPDATA%\\VideoLAN\\VLC\\vlc.exe
         4. Linux   : vlc / cvlc on PATH (e.g. /usr/bin/vlc)
"""

# File extensions that indicate a media stream / file.
MEDIA_EXT_RE = re.compile(
    r"\.(m3u8|mpd|mp4|m4v|mkv|webm|mov|avi|flv|ts|ogv|ogg)(?:[?#/].*)?$",
    re.I,
)
# Static assets we should not bother fetching as "pages".
STATIC_EXT_RE = re.compile(
    r"\.(png|jpe?g|gif|webp|svg|ico|css|woff2?|ttf|otf|eot|json|xml|txt)"
    r"(?:[?#/].*)?$",
    re.I,
)
# Any http(s) URL wrapped in single or double quotes.
QUOTED_URL_RE = re.compile(r"""["'](https?://[^"'\s<>{}|\\^`\[\]]+)["']""", re.I)
# A URL in a src attribute / property, e.g. src="..." or src: "..."
SRC_URL_RE = re.compile(r"""src\s*[:=]\s*["'](https?://[^"'\s>]+)["']""", re.I)


def clean_url(u):
    return u.rstrip(".,;)]}>\"'")


def is_media_url_any(u):
    return bool(MEDIA_EXT_RE.search(u))


def is_static_url(u):
    return bool(STATIC_EXT_RE.search(u))


class StreamRipper:
    def __init__(self, user_agent=DEFAULT_UA, max_depth=10, max_fetches=120,
                 insecure=False, timeout=25):
        self.user_agent = user_agent
        self.max_depth = max_depth
        self.max_fetches = max_fetches
        self.timeout = timeout

        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent})
        if insecure:
            import urllib3
            urllib3.disable_warnings()
            self.session.verify = False

        self.visited = set()
        self.queue = []          # [url, referer, base, kind, depth]
        self.fetch_count = 0

        self.media = {}          # url -> {url, ext, contexts, pages}
        self.nodes = {}          # url -> {parent, type}
        self.nav_log = []
        self.warnings = []

    # ------------------------------------------------------------------ #
    def fetch(self, url, referer=None):
        headers = {}
        if referer:
            headers["Referer"] = referer
        resp = self.session.get(url, headers=headers, timeout=self.timeout,
                                allow_redirects=True)
        ctype = (resp.headers.get("Content-Type") or "").lower()
        return resp.text, resp.url, ctype

    def enqueue(self, url, referer, base, kind, depth):
        url = clean_url(url)
        if not url or url in self.visited:
            return
        if is_media_url_any(url):
            return
        self.queue.append([url, referer, base, kind, depth])

    def run(self, start_url):
        start_url = clean_url(start_url)
        if is_media_url_any(start_url):
            # A raw stream URL was given directly - nothing to crawl.
            self.record_media(start_url, start_url, "input url", None)
            self.nodes[start_url] = {"parent": None, "type": "media"}
            self.nav_log.append(start_url)
            return
        self.queue.append([start_url, None, start_url, "doc", 0])
        while self.queue and self.fetch_count < self.max_fetches:
            url, referer, base, kind, depth = self.queue.pop(0)
            if url in self.visited or depth > self.max_depth:
                continue
            self.visited.add(url)
            self.fetch_count += 1

            try:
                text, final_url, ctype = self.fetch(url, referer)
            except Exception as exc:  # noqa: BLE001
                self.warnings.append("fetch failed %s: %s" % (url, exc))
                continue

            if final_url != url:
                url = final_url
            self.nodes[url] = {"parent": referer,
                               "type": "doc" if kind == "doc" else "js"}
            self.nav_log.append(url)

            if kind == "doc":
                self.process_document(text, url, depth)
            else:
                self.process_script(text, base, url, depth)

    # ------------------------------------------------------------------ #
    # A document (top-level page or iframe). Children are owned by `url`.
    # ------------------------------------------------------------------ #
    def process_document(self, text, url, depth):
        soup = BeautifulSoup(text, "html.parser")
        for tag in soup.find_all("iframe"):
            src = tag.get("src")
            if src and not is_static_url(src):
                absu = urljoin(url, src)
                self.enqueue(absu, url, absu, "doc", depth + 1)
        for tag in soup.find_all("script"):
            src = tag.get("src")
            if src:
                absu = urljoin(url, src)
                self.enqueue(absu, url, url, "script", depth + 1)
            else:
                self.scan_js(tag.get_text(), url, url, depth)
        for tag in soup.find_all(["video", "source"]):
            src = tag.get("src")
            if src and is_media_url_any(urljoin(url, src)):
                self.record_media(urljoin(url, src), url,
                                  "tag:<%s>" % tag.name, url)
        # Safety net: media URLs anywhere in the raw document text.
        for m in QUOTED_URL_RE.finditer(text):
            u = clean_url(m.group(1))
            if is_media_url_any(u):
                self.record_media(urljoin(url, u), url,
                                  self._context(text, m.start()), url)

    # ------------------------------------------------------------------ #
    # A script loaded into document `base`. Iframes it writes are owned by
    # `base` (so they get Referer=base, matching a real browser).
    # ------------------------------------------------------------------ #
    def process_script(self, text, base, url, depth):
        self.scan_js(text, base, url, depth)

    def scan_js(self, text, base, source_page, depth):
        for m in QUOTED_URL_RE.finditer(text):
            u = clean_url(m.group(1))
            if is_media_url_any(u):
                self.record_media(urljoin(base, u), base,
                                  self._context(text, m.start()),
                                  source_page)
        for m in SRC_URL_RE.finditer(text):
            u = clean_url(m.group(1))
            absu = urljoin(base, u)
            if is_media_url_any(absu):
                self.record_media(absu, base,
                                  self._context(text, m.start()),
                                  source_page)
            else:
                # An iframe built via document.write -> a new document owned
                # by the enclosing document `base`.
                self.enqueue(absu, base, absu, "doc", depth + 1)

    def _context(self, text, idx, window=60):
        return text[max(0, idx - window): idx + window].replace("\n", " ").strip()

    # ------------------------------------------------------------------ #
    def record_media(self, url, base, context, source_page):
        if not (url.startswith("http://") or url.startswith("https://")):
            url = urljoin(base, url)
        url = clean_url(url)
        m = MEDIA_EXT_RE.search(url)
        ext = m.group(1).lower() if m else ""
        entry = self.media.get(url)
        if entry is None:
            entry = self.media[url] = {"url": url, "ext": ext,
                                       "contexts": [], "pages": []}
        if context and context not in entry["contexts"]:
            entry["contexts"].append(context[:160])
        if source_page and source_page not in entry["pages"]:
            entry["pages"].append(source_page)

    def score(self, entry):
        s = 0
        ext = entry["ext"]
        if ext == "m3u8":
            s += 100
        elif ext == "mpd":
            s += 90
        elif ext in ("mp4", "m4v", "mkv", "webm", "mov", "avi", "flv"):
            s += 50
        elif ext == "ts":
            s += 10
        ctx = " ".join(entry["contexts"]).lower()
        for kw in ("source", "player", "file", "hls", "m3u8", "stream",
                   "clappr"):
            if kw in ctx:
                s += 8
        return s

    def best(self):
        if not self.media:
            return None
        return max(self.media.values(), key=self.score)

    def chain_to(self, page):
        chain = []
        cur = page
        seen = set()
        while cur and cur in self.nodes and cur not in seen:
            seen.add(cur)
            chain.append(cur)
            cur = self.nodes[cur].get("parent")
        return list(reversed(chain))


class LocalProxy:
    """A tiny local reverse proxy that injects headers (Referer / User-Agent)
    into outgoing requests. Some stream servers gate on those headers, and
    players like VLC have no way to set a Referer for their HTTP input, so we
    front the stream with this proxy and hand the player a local URL."""

    def __init__(self, real_base, referer, ua):
        self.real_base = real_base
        self.referer = referer
        self.ua = ua
        self.server = None
        self.port = None
        self.thread = None

    def start(self):
        proxy = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                local = self.path.lstrip("/")
                real = urljoin(proxy.real_base, local)
                req = urllib.request.Request(
                    real, headers={"User-Agent": proxy.ua,
                                   "Referer": proxy.referer})
                try:
                    resp = urllib.request.urlopen(req, timeout=30)
                except urllib.error.HTTPError as exc:
                    self.send_response(exc.code)
                    self.send_header("Content-Type", "text/plain")
                    self.end_headers()
                    self.wfile.write(("upstream %d" % exc.code).encode())
                    return
                except Exception as exc:  # noqa: BLE001
                    self.send_response(502)
                    self.send_header("Content-Type", "text/plain")
                    self.end_headers()
                    self.wfile.write(("proxy error: %s" % exc).encode())
                    return
                self.send_response(200)
                ctype = resp.headers.get("Content-Type")
                if ctype:
                    self.send_header("Content-Type", ctype)
                self.end_headers()
                try:
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()
                except Exception:  # noqa: BLE001  (client went away)
                    pass
                finally:
                    try:
                        resp.close()
                    except Exception:  # noqa: BLE001
                        pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0),
                                                      Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()
        return "http://127.0.0.1:%d" % self.port

    def stop(self):
        if self.server:
            try:
                self.server.shutdown()
                self.server.server_close()
            except Exception:  # noqa: BLE001
                pass


def find_vlc(prefer_console=False):
    """Locate a VLC executable on PATH or in a known install location.

    Handles Linux (PATH), macOS (app bundle) and Windows (Program Files /
    per-user install).
    """
    ext = ".exe" if os.name == "nt" else ""
    names = ["cvlc", "vlc"] if prefer_console else ["vlc", "cvlc"]
    # 1) On PATH (shutil.which resolves the right extension on Windows).
    for n in names:
        found = shutil.which(n)
        if found:
            return found
    # 2) Known install roots.
    roots = [
        "/Applications/VLC.app/Contents/MacOS",           # macOS
        r"C:\Program Files\VideoLAN\VLC",                 # Windows (64-bit)
        r"C:\Program Files (x86)\VideoLAN\VLC",           # Windows (32-bit)
        os.path.expandvars(r"%LOCALAPPDATA%\VideoLAN\VLC"),  # Windows (per-user)
    ]
    for root in roots:
        for n in names:
            cand = os.path.join(root, n + ext)
            if os.path.isfile(cand):
                return cand
    return None


def play_with_vlc(ripper, best, args):
    stream_url = best["url"]
    # The Referer a real browser would send is the player page that embedded
    # the stream. Fall back to the stream's own origin if we don't have it.
    page = best["pages"][0] if best["pages"] else None
    referer = page
    if not referer:
        p = urlparse(stream_url)
        referer = "%s://%s" % (p.scheme, p.netloc)
    real_base = urljoin(stream_url, "./")

    proxy = LocalProxy(real_base, referer, args.ua)
    local_base = proxy.start()
    filename = stream_url.rstrip("/").split("/")[-1]
    local_url = "%s/%s" % (local_base, filename)

    if args.vlc_bin:
        vlc_bin = args.vlc_bin
    else:
        vlc_bin = find_vlc(prefer_console=args.no_gui)
        if vlc_bin is None:
            print("VLC not found. Install VLC, or pass --vlc-bin <path> "
                  "(e.g. /Applications/VLC.app/Contents/MacOS/VLC on macOS, "
                  "or C:\\Program Files\\VideoLAN\\VLC\\vlc.exe on Windows).",
                  file=sys.stderr)
            return
    cmd = [vlc_bin, "--network-caching=2000"]
    if args.vlc_args:
        cmd += shlex.split(args.vlc_args)
    cmd.append(local_url)

    print("Playing with %s via local proxy" % vlc_bin, file=sys.stderr)
    print("  local   : %s" % local_url, file=sys.stderr)
    print("  upstream: %s" % stream_url, file=sys.stderr)
    print("  referer : %s" % referer, file=sys.stderr)
    print("$ %s" % " ".join(cmd), file=sys.stderr)

    try:
        proc = subprocess.Popen(cmd)
        try:
            proc.wait()
        except KeyboardInterrupt:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
    finally:
        proxy.stop()


def main():
    ap = argparse.ArgumentParser(
        description="Extract the video stream link from a streaming page, "
                    "optionally playing it in VLC.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=USAGE_GUIDE)
    ap.add_argument("url", help="Page URL to rip the stream from")
    ap.add_argument("--all", action="store_true",
                    help="Show every candidate media URL (not just the best)")
    ap.add_argument("--chain", action="store_true",
                    help="Show the navigation chain to the stream")
    ap.add_argument("--json", action="store_true", help="Output JSON")
    ap.add_argument("--insecure", action="store_true",
                    help="Disable TLS certificate verification")
    ap.add_argument("--depth", type=int, default=10, help="Max iframe depth")
    ap.add_argument("--max-fetches", type=int, default=120,
                    help="Max number of pages/scripts to fetch")
    ap.add_argument("--ua", default=DEFAULT_UA, help="User-Agent string")
    ap.add_argument("-q", "--quiet", action="store_true",
                    help="Print only the stream URL (no labels)")
    ap.add_argument("--vlc", action="store_true",
                    help="Play the best stream in VLC (via a local proxy that "
                         "injects the required Referer / User-Agent)")
    ap.add_argument("--no-gui", action="store_true",
                    help="With --vlc, use cvlc (no GUI window)")
    ap.add_argument("--vlc-bin", default=None,
                    help="VLC executable for --vlc (default: auto-detect "
                         "vlc/cvlc on PATH or a known install location)")
    ap.add_argument("--vlc-args", default="",
                    help="Extra arguments to pass to VLC with --vlc")
    args = ap.parse_args()

    # Windows consoles may use a non-UTF-8 codepage; make sure printing never
    # crashes on a stray non-ASCII character in a URL.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass

    ripper = StreamRipper(user_agent=args.ua, max_depth=args.depth,
                          max_fetches=args.max_fetches,
                          insecure=args.insecure)
    ripper.run(args.url)

    best = ripper.best()

    if args.json:
        out = {
            "best": best,
            "all": sorted(ripper.media.values(),
                          key=lambda e: -ripper.score(e)),
            "warnings": ripper.warnings,
            "pages_fetched": ripper.fetch_count,
        }
        if best and args.chain:
            out["chain"] = ripper.chain_to(
                best["pages"][0] if best["pages"] else args.url)
        print(json.dumps(out, indent=2))
        return

    if best is None:
        print("No video stream found.", file=sys.stderr)
        if ripper.warnings:
            print("Warnings:", file=sys.stderr)
            for w in ripper.warnings:
                print("  - %s" % w, file=sys.stderr)
        sys.exit(1)

    if args.chain:
        page = best["pages"][0] if best["pages"] else args.url
        print("Navigation chain:", file=sys.stderr)
        for i, step in enumerate(ripper.chain_to(page)):
            print("  %d. %s" % (i, step), file=sys.stderr)
        print("", file=sys.stderr)

    if args.vlc:
        play_with_vlc(ripper, best, args)
        return

    if args.all:
        for entry in sorted(ripper.media.values(),
                            key=lambda e: -ripper.score(e)):
            print("[%4d] (%s) %s" % (ripper.score(entry), entry["ext"],
                                     entry["url"]))
    else:
        print(best["url"])


if __name__ == "__main__":
    main()
