# ROMarr -- the *arr for games.
#
# Two stages, because of one dependency. ROMarr is stdlib plus `requests`, and
# requests pulls in charset-normalizer, which ships no musl wheel for every
# architecture. On linux/arm/v7 pip therefore compiles it, and a compiler in
# the final image would be ~180MB of toolchain nobody runs. So the build stage
# owns the toolchain and the runtime stage receives only the result.

FROM python:3.13-alpine AS builder

# build-base is needed only where a wheel is missing for the target arch. It is
# unused on amd64/arm64 and load-bearing on armv7. libseccomp-dev is what
# pyseccomp compiles against -- the sandbox ROM Hub confines plugins with.
RUN apk add --no-cache build-base libseccomp-dev

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --prefix=/install -r /tmp/requirements.txt

# ROM Hub, the plugin host the Hub tab drives (issue #10: the image shipped
# the bridge but never the package, so Docker installs reported "rom_hub is
# missing" with nothing to be done about it). Pinned to a commit, because
# "whatever master was when the image built" is not a version.
#
# Skipped on armv7: rom-hub depends on pydantic, whose compiled core ships no
# musl wheel for 32-bit ARM and would drag a Rust toolchain into the build.
# ROMarr itself runs fine there; the Hub tab reports plugins as unavailable,
# which is the truth.
ARG TARGETARCH
RUN if [ "$TARGETARCH" != "arm" ]; then \
      pip install --no-cache-dir --prefix=/install \
        "rom-hub @ https://github.com/BlizzHacker/rom-hub/archive/8e46348783546ee03b00e2c933155ba60d29619d.tar.gz"; \
    fi


FROM python:3.13-alpine

ARG TARGETARCH

# su-exec drops privileges in the entrypoint; it takes numeric ids, so no
# shadow package and no user has to exist in the image. tzdata makes TZ work.
#
# libarchive-tools provides bsdtar, which is what reads .7z and .rar. It is
# not optional cargo: the disc platforms this image now supports ship almost
# entirely as .7z, so without it every PlayStation, PS2 and Wii import fails
# on a format it cannot open. Alpine's busybox `tar` is not a substitute and
# is deliberately not accepted -- see romarr/library.py::_bsdtar.
# libseccomp is the runtime half of the sandbox pyseccomp was built against.
# ROM Hub installs catalogue plugins from pinned repository refs, so git is a
# runtime dependency rather than builder cargo.
RUN apk add --no-cache su-exec tzdata libarchive-tools libseccomp git

COPY --from=builder /install /usr/local

# pyseccomp asks ctypes.util.find_library("seccomp") before opening the C
# library. On Alpine that helper relies on linker/compiler discovery tools
# which belong in the builder, so it returns None even though the runtime
# package correctly installed /usr/lib/libseccomp.so.2. Give this binding the
# normal Alpine SONAME as its fallback. The assertion deliberately breaks the
# build if a future pyseccomp changes the line instead of silently publishing
# another unconstrained image.
RUN python -c "import pathlib,sysconfig; p=pathlib.Path(sysconfig.get_path('purelib'))/'pyseccomp.py'; s=p.read_text(); old='_libseccomp_path = ctypes.util.find_library(\"seccomp\")'; assert s.count(old)==1; p.write_text(s.replace(old, old+' or \"/usr/lib/libseccomp.so.2\"'))"

# Importing the packages is not enough: the broken image imported ROM Hub and
# only failed when the Plugins page asked pyseccomp to find its C library. Load
# a real restrictive filter during the build so that exact failure cannot be
# published again.
# The CI smoke image is native amd64. The multi-arch publish that follows runs
# arm64 under QEMU, where seccomp_load is canceled by the emulator (errno 125)
# even though the same filter loads on native Docker. Prove the actual image on
# the native leg and do not mistake QEMU's build environment for arm64 runtime.
RUN if [ "$TARGETARCH" = "amd64" ]; then \
      python -c "import rom_hub, pyseccomp; from rom_hub.sandbox import install; install()"; \
    fi

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
# ROM_HUB_HOME lives under /config so installed plugins survive a container
# recreation the same way settings do. The application default is a path
# inside the container filesystem, which silently loses every plugin on
# `docker compose pull`.
ENV ROMARR_DATA=/config/romarr.json \
    ROM_HUB_HOME=/config/rom-hub \
    ROMARR_PORT=6868 \
    PUID=1000 \
    PGID=1000 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

VOLUME /config
EXPOSE 6868

# Python is already here, so the healthcheck needs no curl. A non-200 raises.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('ROMARR_PORT','6868')+'/api/health',timeout=4)"

LABEL org.opencontainers.image.title="ROMarr" \
      org.opencontainers.image.description="The *arr for games: request a ROM, ROMarr finds it via Prowlarr, grabs it, and files it into your game library" \
      org.opencontainers.image.source="https://github.com/BlizzHacker/romarr" \
      org.opencontainers.image.licenses="MIT"

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "-m", "romarr"]
