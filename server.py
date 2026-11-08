"""
yt-dlp microservice — a thin HTTP wrapper around the yt-dlp (and gallery-dl) CLI.

Purpose: be the single egress point for all yt-dlp / YouTube traffic. Run this on a
host with a clean (e.g. residential) IP so requests don't hit YouTube's datacenter
bot-wall. Consumers call HTTP instead of shelling out to a local yt-dlp binary, which
means there is exactly one place that talks to YouTube — it cannot be bypassed.

Endpoints (all except /health require `Authorization: Bearer <YTDLP_SERVICE_TOKEN>`):
  GET  /health               -> liveness, no auth
  GET  /version              -> { version }
  POST /json     {url,args?}  -> raw `yt-dlp -j` stdout (-J when requested in args)
  POST /get-url  {url,args?}  -> { urls: [...] }   (`yt-dlp --get-url`)
  POST /download {url,args?}  -> streams the produced media file back
  POST /gallery  {url,cookies?,limit?} -> raw `gallery-dl -j` stdout, verbatim (application/json).
                                 Covers what yt-dlp cannot: image posts and multi-slide
                                 carousels (Instagram et al.), which have no video format
                                 and therefore no yt-dlp result at all. `cookies` is an
                                 optional Netscape jar from a caller that keeps its own
                                 credentials; it beats the disk jar and is never persisted.
  GET  /fetch?url=...         -> streams an allow-listed URL (subtitle tracks,
                                 googlevideo/cdninstagram segments) from THIS host's IP,
                                 forwarding Range so media stays seekable.

`args` is an optional list of extra yt-dlp flags (e.g. ["-f","ba","--audio-format","opus"]).
Flags that would let a caller redirect output, read/write local files or execute commands
are rejected — the service owns output paths AND credentials (see sanitize_args).

Cookies are a SERVER-SIDE concern: callers never send them. Drop Netscape cookie files
into YTDLP_COOKIES_DIR (default /cookies) named after the site — e.g. instagram.txt — and
the service injects `--cookies` itself for matching URLs. See COOKIE_HOSTS. Both CLIs read
the same Netscape jar, so one file serves yt-dlp and gallery-dl alike.
"""
import os
import asyncio
import shutil
import tempfile
import mimetypes
import subprocess
import contextlib
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

TOKEN = os.environ.get("YTDLP_SERVICE_TOKEN", "").strip()
COOKIES_DIR = os.environ.get("YTDLP_COOKIES_DIR", "/cookies").strip()

# Host suffix -> cookie file basename inside COOKIES_DIR. A file that isn't there is simply
# skipped, so a deploy without cookies keeps working exactly as before (anonymous).
COOKIE_HOSTS = (
    (".instagram.com", "instagram.txt"),
    (".youtube.com", "youtube.txt"),
    (".youtu.be", "youtube.txt"),
    (".tiktok.com", "tiktok.txt"),
)

# /fetch is a deliberately narrow relay — only these hosts — so it can never become an
# open proxy / SSRF vector. Covers YouTube pages (timedtext subtitles), media CDNs and
# thumbnails, plus the Instagram/Meta CDNs the Instagram extractor resolves to.
FETCH_ALLOW_SUFFIXES = (
    ".youtube.com",
    ".googlevideo.com",
    ".ytimg.com",
    ".youtube-nocookie.com",
    ".ggpht.com",
    ".cdninstagram.com",
    ".fbcdn.net",
)

VERSION_TIMEOUT = 15
JSON_TIMEOUT = 90
GALLERY_TIMEOUT = 90
# Upper bound for /gallery's `limit`. A feed poller wants the newest handful, not an archive.
MAX_POST_LIMIT = 50
DOWNLOAD_TIMEOUT = 900

# EIN geteilter HTTP-Client für /fetch statt einem pro Request.
#
# Der Relay wird nicht einzeln benutzt, sondern in Serie: die Watch-Party zieht Segment für
# Segment vom selben googlevideo-Host, ein Video-Player scrubbt sich in Range-Happen durch
# dieselbe mp4. Ein Client pro Request wirft die Verbindung danach weg und zahlt jedes Mal
# DNS + TCP + TLS-Handshake neu; mit Pool bleibt sie offen und der nächste Happen startet sofort.
RELAY_LIMITS = httpx.Limits(max_connections=64, max_keepalive_connections=32, keepalive_expiry=60.0)
# read=None: der Body wird gestreamt, ein langsamer Client darf nicht in einen Read-Timeout laufen.
RELAY_TIMEOUT = httpx.Timeout(30.0, read=None)

_relay_client: httpx.AsyncClient | None = None


def relay_client() -> httpx.AsyncClient:
    global _relay_client
    if _relay_client is None or _relay_client.is_closed:
        _relay_client = httpx.AsyncClient(timeout=RELAY_TIMEOUT, follow_redirects=True, limits=RELAY_LIMITS)
    return _relay_client


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    if _relay_client is not None and not _relay_client.is_closed:
        await _relay_client.aclose()


app = FastAPI(title="yt-dlp-service", docs_url=None, redoc_url=None, lifespan=lifespan)


def require_auth(authorization: str | None) -> None:
    # Fail closed: an unconfigured deploy is inert, never an open downloader.
    if not TOKEN:
        raise HTTPException(503, "service not configured: set YTDLP_SERVICE_TOKEN")
    if authorization != f"Bearer {TOKEN}":
        raise HTTPException(401, "unauthorized")


def _run(tool: str, args: list[str], timeout: int) -> subprocess.CompletedProcess:
    try:
        return subprocess.run([tool, *args], capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise HTTPException(504, f"{tool} timed out")


# WICHTIG: beide Runner werden `await`ed und laufen im Threadpool, nicht auf dem Event-Loop.
#
# `subprocess.run` blockiert. Direkt aus einem `async def` heraus aufgerufen steht damit der
# GESAMTE Service, solange yt-dlp läuft — bei /download bis zu 15 Minuten. Betroffen wäre vor
# allem /fetch: ausgerechnet das Video-Streaming (Instagram-Karte, YouTube-Shorts, Watch-Party-
# Segmente) würde hinter jedem Metadaten-Abruf einfrieren. Gemessen: ein 3-Sekunden-Aufruf hat
# den Loop 3,1 Sekunden lang stillgelegt.
async def run_ytdlp(args: list[str], timeout: int) -> subprocess.CompletedProcess:
    return await asyncio.to_thread(_run, "yt-dlp", args, timeout)


async def run_gallery_dl(args: list[str], timeout: int) -> subprocess.CompletedProcess:
    return await asyncio.to_thread(_run, "gallery-dl", args, timeout)


# Flags a caller must never set. Beyond output redirection this covers anything that would
# let the caller reach the filesystem or the service's credentials:
#   --cookies / --cookies-from-browser  -> would override or exfiltrate our cookie jar
#   --load-info-json / --config-location / --batch-file -> read arbitrary local files
#   --exec / --downloader / --external-downloader -> run arbitrary commands
#   --paths / -P / --cache-dir -> write outside the request's temp dir
FORBIDDEN_PREFIXES = (
    "-o", "--output",
    "-P", "--paths",
    "--cookies",
    "--load-info-json", "--load-info",
    "--config-location",
    "-a", "--batch-file",
    "--exec",
    "--downloader", "--external-downloader",
    "--cache-dir",
)


def sanitize_args(args) -> list[str]:
    if args is None:
        return []
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise HTTPException(400, "args must be an array of strings")
    # Output paths and credentials are owned by the service; callers must not touch either.
    # Compare on the flag name only ("--exec=foo" and "--exec foo" are the same thing).
    for a in args:
        flag = a.split("=", 1)[0]
        if flag in FORBIDDEN_PREFIXES:
            raise HTTPException(400, f"args must not set {flag}")
    return args


def cookie_file_for(url: str) -> str | None:
    """Path of the configured cookie jar for `url`, or None if there is none."""
    if not COOKIES_DIR:
        return None
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return None
    for suffix, filename in COOKIE_HOSTS:
        # ".instagram.com" must match both "instagram.com" and "www.instagram.com".
        if host == suffix[1:] or host.endswith(suffix):
            path = os.path.join(COOKIES_DIR, filename)
            return path if os.path.isfile(path) else None
    return None


# A caller-supplied jar is a full Netscape cookie file. Cap it: a real jar for one site is a
# few KB, so anything past this is a mistake or an attempt to make us write a large temp file.
MAX_COOKIE_BYTES = 256 * 1024


@contextlib.contextmanager
def caller_cookie_args(jar: str):
    """
    Yield `--cookies <file>` for a jar the CALLER sent, written to a private temp file.

    This is the inverse of `cookie_args` below and deliberately so: a consumer that keeps its
    credentials in its own store (a database it owns) is the credential owner, and shipping the
    jar with the request beats mirroring it onto this service's disk — nothing can go stale, and
    a redeploy here can't lose it. The jar is never persisted; it dies with the request.

    Note this does NOT loosen `sanitize_args` — `--cookies` remains rejected there, because that
    path would let a caller point the CLI at an arbitrary path on our filesystem. Here we own
    the path and only the file's contents come from outside.
    """
    if not jar:
        yield []
        return
    if len(jar.encode("utf-8", errors="ignore")) > MAX_COOKIE_BYTES:
        raise HTTPException(400, "cookies too large")
    # Netscape jars are tab-separated; a body that has none is a wrong-format upload (JSON,
    # spaces-instead-of-tabs) that would otherwise fail deep inside the CLI with a useless error.
    if "\t" not in jar:
        raise HTTPException(400, "cookies must be a Netscape-format cookie file (tab-separated)")

    tmpdir = tempfile.mkdtemp(prefix="caller-cookies-")
    try:
        dst = os.path.join(tmpdir, "cookies.txt")
        with open(dst, "w", encoding="utf-8") as fh:
            fh.write(jar if jar.endswith("\n") else jar + "\n")
        yield ["--cookies", dst]
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


@contextlib.contextmanager
def cookie_args(url: str):
    """
    Yield the `--cookies <file>` args for `url` (empty list if no jar is configured).

    The jar is COPIED into a private temp dir for the duration of the run. Two reasons,
    both load-bearing:
      1. yt-dlp writes the cookie jar back on exit — pointed at the original it truncates
         it to a bare header on the first request, and the credential is gone for good.
         gallery-dl does the same (its `cookies-update` defaults to on).
      2. Concurrent requests would race on the same file.
    The cost is that refreshed session cookies are discarded; session IDs are long-lived,
    so that is the cheaper side of the trade.
    """
    src = cookie_file_for(url)
    if not src:
        yield []
        return
    tmpdir = tempfile.mkdtemp(prefix="ytdlp-cookies-")
    try:
        dst = os.path.join(tmpdir, "cookies.txt")
        shutil.copyfile(src, dst)
        yield ["--cookies", dst]
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def cli_error(tool: str, p: subprocess.CompletedProcess) -> JSONResponse:
    return JSONResponse(
        {"error": f"{tool} failed", "stderr": p.stderr.decode(errors="replace")[-2000:]},
        status_code=502,
    )


def ytdlp_error(p: subprocess.CompletedProcess) -> JSONResponse:
    return cli_error("yt-dlp", p)


@app.get("/health")
def health():
    return {"ok": True, "token_configured": bool(TOKEN)}


@app.get("/version")
async def version(authorization: str | None = Header(default=None)):
    require_auth(authorization)
    p = await run_ytdlp(["--version"], VERSION_TIMEOUT)
    if p.returncode != 0:
        raise HTTPException(502, "yt-dlp --version failed")
    # gallery-dl is optional in spirit: an image without it still answers, just without a version.
    g = await run_gallery_dl(["--version"], VERSION_TIMEOUT)
    gallery_version = g.stdout.decode().strip() if g.returncode == 0 else None
    # Which cookie jars are actually present — lets callers diagnose "login required" errors
    # without shell access. Authenticated endpoint, so the site list stays internal.
    cookies = sorted({
        filename for _, filename in COOKIE_HOSTS
        if COOKIES_DIR and os.path.isfile(os.path.join(COOKIES_DIR, filename))
    })
    return {
        "version": p.stdout.decode().strip(),
        "gallery_dl_version": gallery_version,
        "cookies": cookies,
    }


@app.post("/json")
async def json_endpoint(request: Request, authorization: str | None = Header(default=None)):
    require_auth(authorization)
    body = await request.json()
    url = (body.get("url") or "").strip()
    args = sanitize_args(body.get("args"))
    if not url:
        raise HTTPException(400, "missing url")
    # -J already dumps the whole playlist; adding -j would also print each entry.
    dump_args = [] if any(a in ("-J", "--dump-single-json") for a in args) else ["-j"]
    with cookie_args(url) as ck:
        p = await run_ytdlp([*dump_args, "--no-warnings", *ck, *args, url], JSON_TIMEOUT)
    if p.returncode != 0:
        return ytdlp_error(p)
    # Pass stdout through verbatim, including the playlist/entries shape of -J.
    return PlainTextResponse(p.stdout.decode(errors="replace"), media_type="application/json")


@app.post("/get-url")
async def get_url(request: Request, authorization: str | None = Header(default=None)):
    require_auth(authorization)
    body = await request.json()
    url = (body.get("url") or "").strip()
    args = sanitize_args(body.get("args"))
    if not url:
        raise HTTPException(400, "missing url")
    with cookie_args(url) as ck:
        p = await run_ytdlp(["--get-url", "--no-warnings", *ck, *args, url], JSON_TIMEOUT)
    if p.returncode != 0:
        return ytdlp_error(p)
    urls = [ln.strip() for ln in p.stdout.decode(errors="replace").splitlines() if ln.strip().startswith("http")]
    return {"urls": urls}


@app.post("/download")
async def download(request: Request, authorization: str | None = Header(default=None)):
    require_auth(authorization)
    body = await request.json()
    url = (body.get("url") or "").strip()
    args = sanitize_args(body.get("args"))
    if not url:
        raise HTTPException(400, "missing url")

    # Fragmentierte Formate (DASH/HLS) laden sonst Häppchen für Häppchen seriell — bei einem
    # Video mit hunderten Fragmenten ist die Leitung dabei die längste Zeit im Leerlauf.
    # Nur als Vorgabe: wer selbst ein -N mitschickt, behält es.
    parallel = [] if any(a.split("=", 1)[0] in ("-N", "--concurrent-fragments") for a in args) else ["-N", "4"]

    workdir = tempfile.mkdtemp(prefix="ytdlp-")
    try:
        out_tmpl = os.path.join(workdir, "media.%(ext)s")
        with cookie_args(url) as ck:
            p = await run_ytdlp(
                [*ck, *parallel, *args, "--no-warnings", "--no-progress", "-o", out_tmpl, url],
                DOWNLOAD_TIMEOUT,
            )
        produced = [os.path.join(workdir, f) for f in os.listdir(workdir)]
        produced = [f for f in produced if os.path.isfile(f)]
        if p.returncode != 0 or not produced:
            shutil.rmtree(workdir, ignore_errors=True)
            return ytdlp_error(p)
        # The largest file is the real media (not a leftover thumbnail / .part).
        path = max(produced, key=os.path.getsize)
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        return FileResponse(
            path,
            media_type=ctype,
            filename=os.path.basename(path),
            background=BackgroundTask(shutil.rmtree, workdir, ignore_errors=True),
        )
    except Exception:
        shutil.rmtree(workdir, ignore_errors=True)
        raise


@app.post("/gallery")
async def gallery(request: Request, authorization: str | None = Header(default=None)):
    """
    `gallery-dl -j <url>` — metadata for post types yt-dlp has no answer for.

    yt-dlp only knows *video*: an Instagram image post or a multi-slide carousel has no
    format list, so `/json` fails on exactly the posts a gallery view is for. gallery-dl
    enumerates every slide (image and video) with its metadata instead, and nothing is
    downloaded — `-j` dumps and exits.

    Unlike the yt-dlp endpoints this takes NO caller args. gallery-dl's flag surface reaches
    the filesystem in more places than yt-dlp's (`-d`, `--config`, `-o key=value` writing
    arbitrary config keys, `--exec`), and no caller has needed a knob here — an allow-list
    of one (`-j`) is cheaper to keep safe than a deny-list.

    Optional `limit`: cap the number of posts via --post-range, for profile/feed URLs.

    Optional `cookies`: a Netscape jar the caller keeps in its own store (see
    `caller_cookie_args`). It wins over a jar on our disk — the caller asked for that
    credential specifically. Without it the usual disk jar applies, so a consumer that has
    none behaves exactly as before.
    """
    require_auth(authorization)
    body = await request.json()
    url = (body.get("url") or "").strip()
    jar = body.get("cookies") or ""
    limit = body.get("limit")
    if not url:
        raise HTTPException(400, "missing url")
    if not isinstance(jar, str):
        raise HTTPException(400, "cookies must be a string")

    # `limit` maps to --post-range and exists for ONE reason: a profile URL would otherwise make
    # gallery-dl page through the account's entire history in a single call — hundreds of
    # requests in a burst, the fastest way to get a cookie flagged. Numeric and bounded, so this
    # stays an allow-list of one flag rather than a caller-controlled argument.
    range_args = []
    if limit is not None:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_POST_LIMIT:
            raise HTTPException(400, f"limit must be an integer between 1 and {MAX_POST_LIMIT}")
        range_args = ["--post-range", f"1-{limit}"]

    with contextlib.ExitStack() as stack:
        ck = (stack.enter_context(caller_cookie_args(jar)) if jar
              else stack.enter_context(cookie_args(url)))
        p = await run_gallery_dl(["-j", *range_args, *ck, url], GALLERY_TIMEOUT)
    if p.returncode != 0:
        return cli_error("gallery-dl", p)
    # Verbatim, like /json: the caller expects gallery-dl's exact `[[type, url, meta], …]` shape.
    return PlainTextResponse(p.stdout.decode(errors="replace"), media_type="application/json")


def content_range_total(value: str | None) -> int | None:
    """Gesamtgröße aus einem `Content-Range: bytes <von>-<bis>/<total>` — oder None (`*`/kaputt)."""
    if not value:
        return None
    _, _, total = value.partition("/")
    return int(total) if total.strip().isdigit() else None


def covers_whole_body(value: str | None) -> bool:
    """True, wenn das 206 den KOMPLETTEN Body enthält (`bytes 0-<total-1>/<total>`)."""
    total = content_range_total(value)
    if total is None or not value.lower().startswith("bytes "):
        return False
    span = value[len("bytes "):].partition("/")[0].strip()
    start, _, end = span.partition("-")
    return start.strip() == "0" and end.strip().isdigit() and int(end) == total - 1


@app.api_route("/fetch", methods=["GET", "HEAD"])
async def fetch(
    url: str = Query(...),
    request: Request = None,
    authorization: str | None = Header(default=None),
):
    require_auth(authorization)
    host = (urlparse(url).hostname or "").lower()
    if not (host == "youtube.com" or any(host.endswith(s) for s in FETCH_ALLOW_SUFFIXES)):
        raise HTTPException(403, f"host not allowed: {host or 'none'}")

    # Ask upstream for uncompressed bytes — we stream raw (aiter_raw), so a gzip body would
    # otherwise reach the caller still-compressed but unlabeled and break text/JSON parsing.
    fwd_headers = {"Accept-Encoding": "identity"}
    # Meta's video CDN 403s a request without a browser User-Agent (gallery-dl sends one for
    # exactly these URLs). Harmless for the other allow-listed hosts, so it goes on every relay.
    fwd_headers["User-Agent"] = "Mozilla/5.0"
    # Forward Range so HLS / googlevideo media stays seekable (206 partial content).
    rng = request.headers.get("range") if request is not None else None
    head_only = request is not None and request.method == "HEAD"
    if rng:
        fwd_headers["Range"] = rng
    else:
        # KEIN Range vom Aufrufer? Trotzdem einen schicken.
        #
        # Metas Video-CDN beantwortet ein blankes GET chunked: ohne Content-Length und ohne
        # Accept-Ranges. Genau dieser Stream ist für Apples AVFoundation unbrauchbar — sie
        # braucht die Länge und die Range-Zusage, um progressives mp4 abzuspielen, und meldet
        # sonst einen generischen Format-/Codec-Fehler (Androids ExoPlayer streamt einfach
        # sequenziell weiter, deshalb fällt es nur auf Apple-Geräten auf).
        #
        # Mit `bytes=0-` liefert dasselbe CDN ein 206 mit `Content-Range: bytes 0-N/N+1` —
        # daraus wird unten wieder ein sauberes 200 mit Content-Length. Bei HEAD reicht ein
        # einzelnes Byte, um dieselbe Auskunft zu bekommen, ohne die Datei anzufassen.
        fwd_headers["Range"] = "bytes=0-0" if head_only else "bytes=0-"

    client = relay_client()
    upstream = await client.send(client.build_request("GET", url, headers=fwd_headers), stream=True)

    passthrough = {
        h: upstream.headers[h]
        # content-encoding is forwarded so that if an upstream ignores our identity request and
        # compresses anyway, the caller (which auto-decompresses) still gets correct bytes.
        for h in ("content-type", "content-length", "content-range", "accept-ranges", "cache-control", "content-encoding")
        if h in upstream.headers
    }
    # Expose the final URL (after redirects) so callers can resolve relative manifest URIs
    # against it — `upstream.url` on the caller side would be this service, not the real host.
    passthrough["x-final-url"] = str(upstream.url)

    status = upstream.status_code
    # Das selbst angefragte Range wieder wegübersetzen: der Aufrufer hat keins geschickt, also
    # darf er auch kein 206 sehen (ein 206 ohne Range-Request ist schlicht ungültig). Was er
    # stattdessen bekommt, ist das, was am blanken GET fehlte: Länge + Accept-Ranges.
    if not rng and status == 206:
        total = content_range_total(upstream.headers.get("content-range"))
        if total is not None and (head_only or covers_whole_body(upstream.headers.get("content-range"))):
            status = 200
            passthrough.pop("content-range", None)
            passthrough["content-length"] = str(total)
            passthrough["accept-ranges"] = "bytes"

    if head_only:
        # Nur die Antwort schließen, nicht den Client — der Pool lebt weiter (siehe relay_client).
        await upstream.aclose()
        return Response(
            status_code=status,
            headers=passthrough,
            media_type=upstream.headers.get("content-type"),
        )

    async def body_iter():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()

    return StreamingResponse(
        body_iter(),
        status_code=status,
        headers=passthrough,
        media_type=upstream.headers.get("content-type"),
    )
