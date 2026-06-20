"""
yt-dlp microservice — a thin HTTP wrapper around the yt-dlp CLI.

Purpose: be the single egress point for all yt-dlp / YouTube traffic. Run this on a
host with a clean (e.g. residential) IP so requests don't hit YouTube's datacenter
bot-wall. Consumers call HTTP instead of shelling out to a local yt-dlp binary, which
means there is exactly one place that talks to YouTube — it cannot be bypassed.

Endpoints (all except /health require `Authorization: Bearer <YTDLP_SERVICE_TOKEN>`):
  GET  /health               -> liveness, no auth
  GET  /version              -> { version }
  POST /json     {url,args?}  -> raw `yt-dlp -j` stdout, verbatim (application/json)
  POST /get-url  {url,args?}  -> { urls: [...] }   (`yt-dlp --get-url`)
  POST /download {url,args?}  -> streams the produced media file back
  GET  /fetch?url=...         -> streams an allow-listed URL (subtitle tracks,
                                 googlevideo segments) from THIS host's IP, forwarding
                                 Range so media stays seekable. Replaces a side proxy.

`args` is an optional list of extra yt-dlp flags (e.g. ["-f","ba","--audio-format","opus"]).
Output flags (-o/--output) are rejected — the service owns output paths.
"""
import os
import shutil
import tempfile
import mimetypes
import subprocess
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from starlette.background import BackgroundTask

TOKEN = os.environ.get("YTDLP_SERVICE_TOKEN", "").strip()

# /fetch is a deliberately narrow relay — only these hosts — so it can never become an
# open proxy / SSRF vector. Covers YouTube pages (timedtext subtitles), media CDNs and
# thumbnails.
FETCH_ALLOW_SUFFIXES = (
    ".youtube.com",
    ".googlevideo.com",
    ".ytimg.com",
    ".youtube-nocookie.com",
    ".ggpht.com",
)

VERSION_TIMEOUT = 15
JSON_TIMEOUT = 90
DOWNLOAD_TIMEOUT = 900

app = FastAPI(title="yt-dlp-service", docs_url=None, redoc_url=None)


def require_auth(authorization: str | None) -> None:
    # Fail closed: an unconfigured deploy is inert, never an open downloader.
    if not TOKEN:
        raise HTTPException(503, "service not configured: set YTDLP_SERVICE_TOKEN")
    if authorization != f"Bearer {TOKEN}":
        raise HTTPException(401, "unauthorized")


def run_ytdlp(args: list[str], timeout: int) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["yt-dlp", *args], capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "yt-dlp timed out")


def sanitize_args(args) -> list[str]:
    if args is None:
        return []
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise HTTPException(400, "args must be an array of strings")
    # Output paths are owned by the service (temp dirs); callers must not set them.
    for a in args:
        if a in ("-o", "--output") or a.startswith("-o") or a.startswith("--output"):
            raise HTTPException(400, "args must not set output (-o/--output)")
    return args


def ytdlp_error(p: subprocess.CompletedProcess) -> JSONResponse:
    return JSONResponse(
        {"error": "yt-dlp failed", "stderr": p.stderr.decode(errors="replace")[-2000:]},
        status_code=502,
    )


@app.get("/health")
def health():
    return {"ok": True, "token_configured": bool(TOKEN)}


@app.get("/version")
def version(authorization: str | None = Header(default=None)):
    require_auth(authorization)
    p = run_ytdlp(["--version"], VERSION_TIMEOUT)
    if p.returncode != 0:
        raise HTTPException(502, "yt-dlp --version failed")
    return {"version": p.stdout.decode().strip()}


@app.post("/json")
async def json_endpoint(request: Request, authorization: str | None = Header(default=None)):
    require_auth(authorization)
    body = await request.json()
    url = (body.get("url") or "").strip()
    args = sanitize_args(body.get("args"))
    if not url:
        raise HTTPException(400, "missing url")
    p = run_ytdlp(["-j", "--no-warnings", *args, url], JSON_TIMEOUT)
    if p.returncode != 0:
        return ytdlp_error(p)
    # Pass stdout through verbatim so the caller sees the exact `-j` shape it expects.
    return PlainTextResponse(p.stdout.decode(errors="replace"), media_type="application/json")


@app.post("/get-url")
async def get_url(request: Request, authorization: str | None = Header(default=None)):
    require_auth(authorization)
    body = await request.json()
    url = (body.get("url") or "").strip()
    args = sanitize_args(body.get("args"))
    if not url:
        raise HTTPException(400, "missing url")
    p = run_ytdlp(["--get-url", "--no-warnings", *args, url], JSON_TIMEOUT)
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

    workdir = tempfile.mkdtemp(prefix="ytdlp-")
    try:
        out_tmpl = os.path.join(workdir, "media.%(ext)s")
        p = run_ytdlp([*args, "--no-warnings", "--no-progress", "-o", out_tmpl, url], DOWNLOAD_TIMEOUT)
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


@app.get("/fetch")
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
    # Forward Range so HLS / googlevideo media stays seekable (206 partial content).
    rng = request.headers.get("range") if request is not None else None
    if rng:
        fwd_headers["Range"] = rng

    client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=None), follow_redirects=True)
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

    async def body_iter():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(
        body_iter(),
        status_code=upstream.status_code,
        headers=passthrough,
        media_type=upstream.headers.get("content-type"),
    )
