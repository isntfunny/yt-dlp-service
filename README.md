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
| GET | `/fetch` | `?url=<allow-listed>` | streams the URL from this host, forwards `Range` |

`args` is an optional list of extra yt-dlp flags, e.g. `["-f","ba","--audio-format","opus"]`.
Output flags (`-o` / `--output`) are rejected — the service owns output paths.

`/fetch` only relays a fixed allow-list of hosts (`*.youtube.com`, `*.googlevideo.com`,
`*.ytimg.com`, `*.youtube-nocookie.com`, `*.ggpht.com`) so it can't become an open proxy.
Use it for subtitle (`timedtext`) tracks and googlevideo media segments that are bound to
the resolving host's IP.

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
```

## Configuration

| Env | Required | Default | Notes |
|-----|----------|---------|-------|
| `YTDLP_SERVICE_TOKEN` | **yes** | – | Bearer token. If unset, protected endpoints return `503` (fail-closed). |
| `YTDLP_SERVICE_PORT` | no | `8000` | Host port (compose only). |

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
public internet. yt-dlp is pinned by the image build; rebuild to update it.
