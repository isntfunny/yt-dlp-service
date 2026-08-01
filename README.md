# yt-dlp-service

A tiny HTTP wrapper around the [yt-dlp](https://github.com/yt-dlp/yt-dlp) CLI.

## Why

It exists to be the **single egress point** for all yt-dlp / YouTube traffic. Run it on a
host with a clean (e.g. residential) IP and every consumer calls it over HTTP instead of
shipping a `yt-dlp` binary and talking to YouTube directly. Benefits:

- **One choke point** — no module can bypass it; cookies/IP concerns live in exactly one place.
- **No bot-wall** — datacenter IPs get YouTube's "Sign in to confirm you're not a bot"; a
  residential host does not. Resolve once, here.
- **No binary in your app image** — consumers drop the `yt-dlp` (and its update churn).

## Endpoints

All except `/health` require `Authorization: Bearer $YTDLP_SERVICE_TOKEN`.

| Method | Path | Body / Query | Returns |
|--------|------|--------------|---------|
| GET | `/health` | – | `{ ok, token_configured }` (no auth) |
| GET | `/version` | – | `{ version }` |
| POST | `/json` | `{ url, args? }` | raw `yt-dlp -j` stdout, verbatim |
| POST | `/get-url` | `{ url, args? }` | `{ urls: [...] }` (`--get-url`) |
| POST | `/download` | `{ url, args? }` | the produced media file (streamed) |
| POST | `/gallery` | `{ url, cookies?, limit? }` | raw `gallery-dl -j` stdout, verbatim |
| GET | `/fetch` | `?url=<allow-listed>` | streams the URL from this host, forwards `Range` |

`args` is an optional list of extra yt-dlp flags, e.g. `["-f","ba","--audio-format","opus"]`.
Output flags (`-o` / `--output`) are rejected — the service owns output paths.

`/fetch` only relays a fixed allow-list of hosts (`*.youtube.com`, `*.googlevideo.com`,
`*.ytimg.com`, `*.youtube-nocookie.com`, `*.ggpht.com`, `*.cdninstagram.com`, `*.fbcdn.net`)
so it can't become an open proxy. Use it for subtitle (`timedtext`) tracks and media
segments (googlevideo, Instagram/Meta CDN) that are bound to the resolving host's IP.

`/gallery` is the one endpoint that isn't yt-dlp: it runs **gallery-dl** instead, because
yt-dlp only knows *video*. An Instagram image post or a multi-slide carousel has no format
list, so `/json` fails on precisely those posts — gallery-dl enumerates every slide (image
*and* video) with metadata instead. `-j` dumps and exits; nothing is downloaded. It takes
**no `args`**: gallery-dl's flags reach the filesystem in more places than yt-dlp's (`-d`,
`--config`, `-o key=value`, `--exec`), so the allow-list is just `-j` plus the cookie jar.
It shares the cookie mechanism below — same Netscape jar, both CLIs read it.

`limit` (1–50) caps the number of posts via `--post-range`. Use it for **profile URLs**: without
it, `gallery-dl -j https://www.instagram.com/someone/` pages through the account's entire
history in one call — hundreds of requests in a burst, the fastest way to get a cookie flagged.
A feed poller wants the newest handful, so pass e.g. `{"limit": 5}`.

### Examples

```bash
TOKEN=...   # = YTDLP_SERVICE_TOKEN
BASE=http://localhost:8000

# metadata (raw -j)
curl -s -X POST $BASE/json -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"url":"https://www.youtube.com/watch?v=A0BB_ca_URs"}'

# resolved stream URL(s)
curl -s -X POST $BASE/get-url -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -d '{"url":"https://www.twitch.tv/somechannel"}'

# download a clip as opus
curl -s -X POST $BASE/download -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"url":"https://youtu.be/A0BB_ca_URs","args":["--download-sections","*20-90","-f","ba","--audio-format","opus"]}' \
  -o clip.opus

# relay a subtitle track from this host's IP
curl -s "$BASE/fetch?url=https://www.youtube.com/api/timedtext?..." -H "Authorization: Bearer $TOKEN"

# every slide of an Instagram carousel (needs cookies/instagram.txt)
curl -s -X POST $BASE/gallery -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"url":"https://www.instagram.com/p/ABCdefGhijk/"}'
```

## Cookies

Some sites (Instagram, and increasingly YouTube for age/bot-gated videos) reject anonymous
requests. Cookies are a **server-side-only** concern — callers never send them, and `args`
cannot set `--cookies` (rejected by `sanitize_args`, see Security). Instead:

1. Export a **Netscape-format** cookie jar for a throwaway/burner account (e.g. via a browser
   extension like "Get cookies.txt", or `yt-dlp --cookies-from-browser <browser> --cookies out.txt
   <any URL on that site>`).
2. Drop it into the directory pointed to by `YTDLP_COOKIES_DIR` (default `/cookies`, mounted as
   `./cookies` in `docker-compose.yml`), named after the site:

   | File | Matches |
   |------|---------|
   | `instagram.txt` | `*.instagram.com` |
   | `youtube.txt` | `*.youtube.com`, `*.youtu.be` |
   | `tiktok.txt` | `*.tiktok.com` |

3. That's it — `/json`, `/get-url`, `/download` and `/gallery` inject `--cookies <jar>` automatically for
   any URL whose host matches. A site without a matching file behaves exactly as before
   (anonymous). `GET /version` (authenticated) lists which jars are currently present, for
   diagnosing "login required" errors without shell access.

The jar is **copied** into a private temp dir per request, never used in place: yt-dlp
rewrites the cookie file it's given on exit, so pointing it at the original would truncate it
to an empty header after the very first request (gallery-dl does the same — its
`cookies-update` is on by default). The copy also sidesteps races between
concurrent requests. The tradeoff is that refreshed session cookies aren't persisted back —
acceptable since session IDs for these sites are long-lived (Instagram: ~1 year).

Use a burner account, not a personal one — cookie-authenticated scraping risks the account
getting rate-limited or banned.

### Caller-supplied jars

`/gallery` also accepts the jar **in the request** as `cookies` (Netscape text, ≤256 KB). It
takes precedence over the disk jar and is written to a temp file that dies with the request —
nothing is persisted here.

This is for consumers that already have a durable store of their own: this service is
stateless and gets rebuilt whenever yt-dlp or gallery-dl need a bump, and a jar in its volume
is exactly what gets lost or forgotten in the process. A caller that keeps the jar in its own
database can hand it over per request instead, and nothing can go stale in between. Callers
without one are unaffected — no field, no change in behaviour.

Note this does **not** loosen `sanitize_args`: `--cookies` stays rejected there, because that
flag points the CLI at an arbitrary path on *our* filesystem. Here the service owns the path
and only the file's contents come from outside.

## Configuration

| Env | Required | Default | Notes |
|-----|----------|---------|-------|
| `YTDLP_SERVICE_TOKEN` | **yes** | – | Bearer token. If unset, protected endpoints return `503` (fail-closed). |
| `YTDLP_SERVICE_PORT` | no | `8000` | Host port (compose only). |
| `YTDLP_COOKIES_DIR` | no | `/cookies` | Directory of Netscape cookie files, see Cookies. Empty/missing dir = no cookies used, no error. |

## Run

```bash
docker compose up --build        # set YTDLP_SERVICE_TOKEN in the environment / .env first
# or
docker build -t yt-dlp-service .
docker run -p 8000:8000 -e YTDLP_SERVICE_TOKEN=secret yt-dlp-service
```

## Security

The service can download arbitrary media — **always set a token** and don't expose it
unauthenticated. Ideally bind it to a private network (VPN/WireGuard) rather than the
public internet. yt-dlp and gallery-dl are pinned by the image build; rebuild to update them.
