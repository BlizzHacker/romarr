# ROMarr -- the *arr for games.
#
# Two stages, because of one dependency. ROMarr is stdlib plus `requests`, and
# requests pulls in charset-normalizer, which ships no musl wheel for every
# architecture. On linux/arm/v7 pip therefore compiles it, and a compiler in
# the final image would be ~180MB of toolchain nobody runs. So the build stage
# owns the toolchain and the runtime stage receives only the result.

FROM python:3.13-alpine AS builder

# build-base is needed only where a wheel is missing for the target arch. It is
# unused on amd64/arm64 and load-bearing on armv7.
RUN apk add --no-cache build-base

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --prefix=/install -r /tmp/requirements.txt


FROM python:3.13-alpine

# su-exec drops privileges in the entrypoint; it takes numeric ids, so no
# shadow package and no user has to exist in the image. tzdata makes TZ work.
RUN apk add --no-cache su-exec tzdata

COPY --from=builder /install /usr/local

WORKDIR /app
COPY romarr/ /app/romarr/
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh && mkdir -p /config /roms

# Paths chosen for the container, not the host. ROMARR_DATA is a file, so its
# directory is what gets mounted.
#
# LIBRARY_PATH is deliberately NOT set here. It has an older alias,
# ROMM_LIBRARY, and an image ENV beats the application's fallback chain -- so
# setting it here would silently ignore the ROMM_LIBRARY an existing install
# brought with it. The entrypoint defaults it only when neither name is given.
ENV ROMARR_DATA=/config/romarr.json \
    ROMARR_PORT=7878 \
    PUID=1000 \
    PGID=1000 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

VOLUME /config
EXPOSE 7878

# Python is already here, so the healthcheck needs no curl. A non-200 raises.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('ROMARR_PORT','7878')+'/api/health',timeout=4)"

LABEL org.opencontainers.image.title="ROMarr" \
      org.opencontainers.image.description="The *arr for games: request a ROM, ROMarr finds it via Prowlarr, grabs it, and files it into your game library" \
      org.opencontainers.image.source="https://github.com/BlizzHacker/romarr" \
      org.opencontainers.image.licenses="MIT"

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "-m", "romarr"]
