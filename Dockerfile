FROM python:3.12-slim

# ffmpeg is required by yt-dlp for muxing / --recode / --audio-format.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# A JavaScript runtime is no longer optional for YouTube: yt-dlp solves YouTube's JS
# challenges through EJS, and without a runtime it warns that extraction is deprecated and
# "some formats may be missing" — which then costs extra player requests and retries.
# Deno is the one enabled by default (>= 2.3.0), so it needs no flag. See
# https://github.com/yt-dlp/yt-dlp/wiki/EJS
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

EXPOSE 8000

# yt-dlp and gallery-dl are pinned by the image build — rebuild the image to bump them.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health').status==200 else 1)"

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
