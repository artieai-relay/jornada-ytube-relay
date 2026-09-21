#!/usr/bin/env python3
"""Jornada YouTube search + download relay (prototype).

Plain-HTTP web service for the HP Jornada 720 (Pocket IE cannot do HTTPS):
  GET /              -> search form
  GET /search?q=...  -> up to 10 results: thumbnail, title, uploader,
                        duration, description snippet, download link
  GET /thumb?u=<url> -> proxies an i.ytimg.com thumbnail over HTTP
  GET /get?id=<vid>  -> downloads the video, transcodes to the proven
                        Jornada profile (MPEG-4 320xN @ 8fps, AAC 96k
                        stereo MP4), serves it as a download.
  Native-app API (plain text, for the YouTubeCE Jornada app):
  GET /api/search?q=.. -> lines of "<id>\t<secs>\t<title>"
  GET /api/ready?id=.. -> "READY" or "WAIT"
  GET /play?id=<vid>   -> streams the MP4 inline (video/mp4, ranges OK);
                        starts a background conversion and answers 202
                        "CONVERTING" when not cached yet
  GET /v/<vid>.mp4     -> same as /play (extension-friendly URL)

Stdlib only. Requires: yt-dlp, ffmpeg, ffprobe on PATH.

Security notes:
  - /thumb only proxies hosts ending in .ytimg.com (not an open proxy).
  - /get only accepts 11-char YouTube video ids.
  - Converted files are cached by id; cache dir is size-capped.
  - Videos longer than MAX_DURATION are refused (conversion cost guard).
"""
import html
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(BASE, "cache")
TMP_DIR = os.path.join(BASE, "tmp")
os.makedirs(CACHE_DIR, exist_ok=True)
os.makedirs(TMP_DIR, exist_ok=True)

YTDLP = "yt-dlp"
SEARCH_RESULTS = 10
SEARCH_TIMEOUT = 120
MAX_DURATION = 900          # 15 minutes
CACHE_MAX_BYTES = 2 * 1024**3
THUMB_TIMEOUT = 15
DESC_SNIPPET = 180
ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

# ---- simple per-IP rate limiting for the expensive endpoints ----
_rl = {}
_rl_lock = threading.Lock()
def rate_ok(ip, key, limit, window):
    now = time.time()
    with _rl_lock:
        arr = _rl.setdefault((ip, key), [])
        while arr and now - arr[0] > window:
            arr.pop(0)
        if len(arr) >= limit:
            return False
        arr.append(now)
        return True

_conv_sem = threading.Semaphore(2)   # max 2 concurrent conversions
_conv_locks = {}
_conv_locks_guard = threading.Lock()

def run(cmd, timeout):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

def esc(s):
    return html.escape("" if s is None else str(s), quote=True)

def fmt_dur(secs):
    try:
        secs = int(secs or 0)
    except (TypeError, ValueError):
        return ""
    return "%d:%02d" % (secs // 60, secs % 60)

def safe_filename(title, vid):
    t = re.sub(r"[^A-Za-z0-9 _.-]", "", (title or "video"))[:40].strip()
    return (t or "video") + "-" + vid + ".mp4"

# ---------------- YouTube access via yt-dlp ----------------

def yt_search(query):
    """Returns list of dicts: id,title,uploader,duration,description."""
    cmd = [YTDLP, "--no-warnings", "--skip-download", "--dump-single-json",
           "ytsearch%d:%s" % (SEARCH_RESULTS, query)]
    try:
        p = run(cmd, SEARCH_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise RuntimeError("search timed out")
    if p.returncode != 0 or not p.stdout.strip():
        err = (p.stderr or "")[:200]
        if "not a bot" in err or "Sign in" in err:
            raise RuntimeError("youtube_botwall")
        raise RuntimeError("search failed: " + err)
    try:
        data = json.loads(p.stdout)
    except json.JSONDecodeError:
        raise RuntimeError("search returned bad data")
    out = []
    for e in data.get("entries") or []:
        if not e or not e.get("id"):
            continue
        out.append({
            "id": e["id"],
            "title": e.get("title") or "(no title)",
            "uploader": e.get("uploader") or e.get("channel") or "",
            "duration": e.get("duration"),
            "description": (e.get("description") or "").strip(),
        })
    return out

def yt_meta(vid):
    cmd = [YTDLP, "--no-warnings", "--skip-download", "--dump-single-json", vid]
    try:
        p = run(cmd, 90)
    except subprocess.TimeoutExpired:
        raise RuntimeError("metadata timed out")
    if p.returncode != 0 or not p.stdout.strip():
        err = (p.stderr or "")[:200]
        if "not a bot" in err or "Sign in" in err:
            raise RuntimeError("youtube_botwall")
        raise RuntimeError("metadata failed: " + err)
    return json.loads(p.stdout)

def download_src(vid, dest):
    fmt = ("bv*[height<=480][ext=mp4]+ba[ext=m4a]"
           "/b[height<=480][ext=mp4]/b[height<=480]/b")
    # Socket timeout + limited retries: YouTube sometimes stalls
    # datacenter connections to zero throughput instead of closing them.
    cmd = [YTDLP, "--no-warnings", "--socket-timeout", "20",
           "--retries", "5", "-f", fmt, "-o", dest, vid]
    try:
        p = run(cmd, 600)
    except subprocess.TimeoutExpired:
        raise RuntimeError("download timed out")
    if p.returncode != 0:
        err = (p.stderr or "")[:200]
        if "not a bot" in err or "Sign in" in err:
            raise RuntimeError("youtube_botwall")
        raise RuntimeError("download failed: " + err)
    # -o with %(ext)s not used; yt-dlp may append .mp4 itself
    if not os.path.exists(dest) and os.path.exists(dest + ".mp4"):
        dest = dest + ".mp4"
    return dest

def transcode(src, dst):
    cmd = ["ffmpeg", "-hide_banner", "-y", "-v", "error",
           "-i", src,
           "-vf", "scale=320:-2", "-r", "8",
           "-c:v", "mpeg4", "-q:v", "5",
           "-c:a", "aac", "-b:a", "96k", "-ac", "2", "-ar", "44100",
           "-movflags", "+faststart", dst]
    try:
        p = run(cmd, 900)
    except subprocess.TimeoutExpired:
        raise RuntimeError("conversion timed out")
    if p.returncode != 0 or not os.path.exists(dst):
        raise RuntimeError("conversion failed")

def cache_path(vid):
    return os.path.join(CACHE_DIR, vid + ".mp4")

def evict_cache():
    files = []
    total = 0
    for f in os.listdir(CACHE_DIR):
        fp = os.path.join(CACHE_DIR, f)
        try:
            st = os.stat(fp)
        except OSError:
            continue
        files.append((st.st_mtime, st.st_size, fp))
        total += st.st_size
    files.sort()
    for _, size, fp in files:
        if total <= CACHE_MAX_BYTES:
            break
        try:
            os.remove(fp)
            total -= size
        except OSError:
            pass

_thumb_cache = {}
_thumb_guard = threading.Lock()

def fetch_thumb(url):
    with _thumb_guard:
        hit = _thumb_cache.get(url)
        if hit and time.time() - hit[0] < 86400:
            return hit[1]
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=THUMB_TIMEOUT) as r:
        data = r.read(512 * 1024)
        ctype = r.headers.get_content_type()
    if not data.startswith(b"\xff\xd8"):
        raise RuntimeError("not a jpeg")
    with _thumb_guard:
        _thumb_cache[url] = (time.time(), (data, ctype))
        if len(_thumb_cache) > 200:
            _thumb_cache.pop(next(iter(_thumb_cache)))
    return data, ctype

# ---------------- HTML pages (Pocket IE 4 friendly: no JS, no CSS) ----------------

def page(title, body):
    return ("<html><head><title>%s</title></head>"
            '<body bgcolor="#ffffff" text="#000000" link="#0000cc">'
            "%s</body></html>" % (esc(title), body)).encode("utf-8")

def front_page():
    return page("Jornada YouTube",
        '<center><h2>Jornada YouTube</h2></center><hr>'
        "<p>Search YouTube, then download any video as a small MP4 "
        "that plays on the HP Jornada 720.</p>"
        '<form method="get" action="/search">'
        '<p>Search: <input type="text" name="q" size="30" maxlength="100"> '
        '<input type="submit" value="Search"></p></form>'
        "<hr><p><small>Videos are converted to 320-wide MPEG-4 at 8 fps "
        "with AAC audio. Long videos (over 15 min) are refused. "
        "Plain HTTP on purpose: the 720 cannot do modern HTTPS.</small></p>")

def botwall_page():
    return page("YouTube check",
        "<h2>YouTube asked for verification</h2>"
        "<p>YouTube briefly blocked the download (bot check). "
        "Wait a minute and try again.</p>"
        '<p><a href="/">Back to search</a></p>')

def error_page(msg):
    return page("Error", "<h2>Sorry</h2><p>%s</p>"
                         '<p><a href="/">Back to search</a></p>' % esc(msg))

def results_page(query, results):
    parts = ['<h2>Results for "%s"</h2>' % esc(query),
             '<p><a href="/">New search</a></p><hr>']
    if not results:
        parts.append("<p>No videos found.</p>")
    for r in results:
        vid = r["id"]
        thumb = "/thumb?u=" + urllib.parse.quote_plus(
            "https://i.ytimg.com/vi/%s/default.jpg" % vid, safe="")
        desc = r["description"]
        if len(desc) > DESC_SNIPPET:
            desc = desc[:DESC_SNIPPET].rstrip() + "..."
        parts.append(
            '<table cellpadding="4"><tr>'
            '<td valign="top"><img src="%s" width="120" height="90"></td>'
            '<td valign="top"><b>%s</b><br><small>%s &middot; %s</small><br>%s<br>'
            '<b><a href="/get?id=%s">Download for Jornada (MP4)</a></b>'
            "</td></tr></table><hr>"
            % (thumb, esc(r["title"]), esc(r["uploader"]),
               esc(fmt_dur(r["duration"])), esc(desc), esc(vid)))
    parts.append('<p><a href="/">New search</a></p>')
    return page("Results: " + query, "".join(parts))

def converting_page(vid, title):
    # Pocket IE understands meta refresh; poll until the file is ready.
    return page("Converting...",
        '<meta http-equiv="refresh" content="8;url=/get?id=%s">'
        "<h2>Converting video...</h2>"
        "<p><b>%s</b></p>"
        "<p>The video is downloading and being converted for the Jornada. "
        "This page refreshes automatically; your download will start "
        "when it is ready. Leave this page open.</p>"
        % (esc(vid), esc(title)))

# ---------------- request handler ----------------

class Handler(BaseHTTPRequestHandler):
    server_version = "JornadaTube/0.2"

    def log_message(self, *a):
        sys.stderr.write("%s %s\n" % (self.address_string(), a[0] % a[1:]))

    def send_html(self, body, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_text(self, body, status=200):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        path = url.path
        qs = urllib.parse.parse_qs(url.query)
        ip = self.client_address[0]

        if path == "/" :
            self.send_html(front_page())
            return

        if path == "/search":
            q = (qs.get("q", [""])[0] or "").strip()[:100]
            if not q:
                self.send_html(front_page())
                return
            if not rate_ok(ip, "search", 20, 3600):
                self.send_html(error_page("Too many searches - wait a while and try again."), 429)
                return
            try:
                results = yt_search(q)
            except RuntimeError as e:
                if str(e) == "youtube_botwall":
                    self.send_html(botwall_page(), 503)
                else:
                    self.send_html(error_page(str(e)), 502)
                return
            self.send_html(results_page(q, results))
            return

        if path == "/thumb":
            u = qs.get("u", [""])[0]
            try:
                parts = urllib.parse.urlsplit(u)
            except ValueError:
                parts = None
            host = (parts.hostname or "") if parts else ""
            if (not parts or parts.scheme != "https"
                    or not host.endswith(".ytimg.com")):
                self.send_error(400, "bad thumbnail url")
                return
            try:
                data, ctype = fetch_thumb(u)
            except Exception as e:
                self.send_error(502, "thumbnail fetch failed")
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(data)
            return

        if path == "/get":
            vid = qs.get("id", [""])[0]
            if not ID_RE.match(vid):
                self.send_error(400, "bad video id")
                return
            if not rate_ok(ip, "get", 30, 3600):
                self.send_html(error_page("Too many downloads - wait a while and try again."), 429)
                return
            cp = cache_path(vid)
            if os.path.exists(cp):
                self.serve_file(cp, vid, None)
                return
            with _conv_locks_guard:
                lock = _conv_locks.setdefault(vid, threading.Lock())
            if not lock.acquire(blocking=False):
                # another thread is converting it; show the waiting page
                self.send_html(converting_page(vid, vid))
                return
            try:
                # re-check cache under the lock
                if os.path.exists(cp):
                    self.serve_file(cp, vid, None)
                    return
                try:
                    meta = yt_meta(vid)
                except RuntimeError as e:
                    if str(e) == "youtube_botwall":
                        self.send_html(botwall_page(), 503)
                    else:
                        self.send_html(error_page(str(e)), 502)
                    return
                dur = meta.get("duration") or 0
                if dur > MAX_DURATION:
                    self.send_html(error_page(
                        "That video is %s long - over the 15 minute limit."
                        % fmt_dur(dur)), 400)
                    return
                title = meta.get("title") or "video"
                # The Jornada's browser times out on slow pages, so hand
                # back a self-refreshing wait page and convert in a
                # background thread.
                t = threading.Thread(target=self.convert_bg,
                                     args=(vid, title), daemon=True)
                t.start()
                self.send_html(converting_page(vid, title))
            finally:
                lock.release()
            return

        if path == "/ready":
            # JSON poll endpoint: /ready?id=<vid> -> {"ready": true/false}
            vid = qs.get("id", [""])[0]
            ok = bool(ID_RE.match(vid)) and os.path.exists(cache_path(vid))
            body = json.dumps({"ready": ok}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # ---- native-app API (plain text, tab-separated) ----

        if path == "/api/search":
            q = (qs.get("q", [""])[0] or "").strip()[:100]
            if not q:
                self.send_text("", 200)
                return
            if not rate_ok(ip, "apisearch", 20, 3600):
                self.send_text("ERROR rate limited", 429)
                return
            try:
                results = yt_search(q)
            except RuntimeError as e:
                if str(e) == "youtube_botwall":
                    self.send_text("ERROR botwall", 503)
                else:
                    self.send_text("ERROR " + str(e)[:120], 502)
                return
            lines = []
            for r in results:
                title = re.sub(r"[\t\r\n]+", " ", r["title"])[:200].strip()
                dur = int(r["duration"] or 0)
                lines.append("%s\t%d\t%s" % (r["id"], dur, title))
            self.send_text("\n".join(lines), 200)
            return

        if path == "/api/ready":
            vid = qs.get("id", [""])[0]
            ok = bool(ID_RE.match(vid)) and os.path.exists(cache_path(vid))
            self.send_text("READY" if ok else "WAIT", 200)
            return

        # /play?id=<vid> and /v/<vid>.mp4: stream the converted file inline
        # (Content-Type video/mp4, no attachment) so a media player can
        # play it progressively. Starts a background conversion (202
        # "CONVERTING") when the file is not cached yet.
        vid = None
        if path == "/play":
            vid = qs.get("id", [""])[0]
        else:
            m = re.match(r"^/v/([A-Za-z0-9_-]{11})\.mp4$", path)
            if m:
                vid = m.group(1)
        if vid is not None:
            if not ID_RE.match(vid):
                self.send_error(400, "bad video id")
                return
            if not rate_ok(ip, "play", 30, 3600):
                self.send_text("ERROR rate limited", 429)
                return
            state, msg = self.start_convert(vid)
            if state == "ready":
                self.serve_file(cache_path(vid), vid, None, inline=True)
            elif state == "converting":
                self.send_text("CONVERTING", 202)
            else:
                self.send_text("ERROR " + msg,
                               503 if msg == "botwall" else 502)
            return

        self.send_error(404)

    def convert_bg(self, vid, title):
        """Runs in a worker thread; semaphore released when done."""
        if not _conv_sem.acquire(blocking=False):
            sys.stderr.write("converter busy, dropping %s\n" % vid)
            return
        try:
            tmp_src = os.path.join(TMP_DIR, vid + ".src")
            try:
                src = download_src(vid, tmp_src)
                tmp_out = os.path.join(TMP_DIR, vid + ".mp4")
                transcode(src, tmp_out)
                os.replace(tmp_out, cache_path(vid))
                evict_cache()
            finally:
                for f in (tmp_src, tmp_src + ".mp4"):
                    try:
                        os.remove(f)
                    except OSError:
                        pass
        except Exception as e:
            sys.stderr.write("convert failed for %s: %s\n" % (vid, e))
        finally:
            _conv_sem.release()

    def serve_file(self, path, vid, title, inline=False):
        size = os.path.getsize(path)
        # Range support so media players can seek and start progressively.
        start, end = 0, size - 1
        range_hdr = self.headers.get("Range")
        if range_hdr:
            m = re.match(r"bytes=(\d*)-(\d*)$", range_hdr.strip())
            if m:
                a, b = m.group(1), m.group(2)
                if a and b:
                    start, end = int(a), min(int(b), size - 1)
                elif a:
                    start, end = int(a), size - 1
                elif b:
                    start, end = max(0, size - int(b)), size - 1
                start = max(0, min(start, size - 1))
                end = max(start, min(end, size - 1))
        length = end - start + 1
        if range_hdr and (start != 0 or end != size - 1):
            self.send_response(206)
            self.send_header("Content-Range",
                             "bytes %d-%d/%d" % (start, end, size))
        else:
            self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if not inline:
            fname = safe_filename(title, vid)
            self.send_header("Content-Disposition",
                             'attachment; filename="%s"' % fname)
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(65536, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def start_convert(self, vid):
        """Kick off a background conversion like /get does.

        Returns a (status, text) pair for API-style answers:
        ("ready", None) if it became ready, ("converting", None) if a
        worker was started or is already running, ("error", msg) if the
        video cannot be converted."""
        cp = cache_path(vid)
        if os.path.exists(cp):
            return ("ready", None)
        with _conv_locks_guard:
            lock = _conv_locks.setdefault(vid, threading.Lock())
        if not lock.acquire(blocking=False):
            return ("converting", None)
        try:
            if os.path.exists(cp):
                return ("ready", None)
            try:
                meta = yt_meta(vid)
            except RuntimeError as e:
                if str(e) == "youtube_botwall":
                    return ("error", "botwall")
                return ("error", str(e)[:120])
            if (meta.get("duration") or 0) > MAX_DURATION:
                return ("error", "too long (over 15 minutes)")
            title = meta.get("title") or "video"
            t = threading.Thread(target=self.convert_bg,
                                 args=(vid, title), daemon=True)
            t.start()
            return ("converting", None)
        finally:
            lock.release()


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8099
    # Bind 127.0.0.1 by default; pass "lan" as argv[2] to listen on all
    # interfaces so the Jornada can reach the server over the LAN.
    host = "0.0.0.0" if len(sys.argv) > 2 and sys.argv[2] == "lan" else "127.0.0.1"
    srv = ThreadingHTTPServer((host, port), Handler)
    sys.stderr.write("JornadaTube listening on %s:%d\n" % (host, port))
    srv.serve_forever()

if __name__ == "__main__":
    main()
