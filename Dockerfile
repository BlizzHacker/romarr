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
        "rom-hub @ https://github.com/BlizzHacker/rom-hub/archive/a563e70dd3792568c97cc8342935d359e0a7a7ea.tar.gz"; \
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
# rclone is the cloud storage engine (romarr/cloud_rclone.py): Google Drive,
# OneDrive, Dropbox, MEGA, S3 and the rest. fuse3 lets it mount a cloud folder
# as a library root when the container is given /dev/fuse.
RUN apk add --no-cache su-exec tzdata libarchive-tools libseccomp git rclone fuse3

# Plugins ROMarr ships preinstalled (romarr/compressatorium_plugin.py,
# romarr/arcade_punks.py). Each is
# a git checkout at a pinned tag, because ROM Hub installs plugins from git;
# ROMarr installs it into ROM_HUB_HOME on first start, with no network.
# Not on armv7, which has no ROM Hub.
ARG COMPRESSATORIUM_PLUGIN_REF=v0.1.0
ARG ARCADE_PUNKS_PLUGIN_REF=v0.1.0
RUN if [ "$TARGETARCH" != "arm" ]; then \
      git clone --quiet --depth 1 --branch "$COMPRESSATORIUM_PLUGIN_REF" \
        https://github.com/BlizzHacker/rom-hub-compressatorium \
        /opt/romarr/bundled-plugins/compressatorium && \
      git clone --quiet --depth 1 --branch "$ARCADE_PUNKS_PLUGIN_REF" \
        https://github.com/BlizzHacker/rom-hub-arcade-punks \
        /opt/romarr/bundled-plugins/arcade-punks; \
    fi
# The Mister Download Manager plugin (romarr/mdm_plugin.py), bundled the same way.
ARG MDM_PLUGIN_REF=v0.1.0
RUN if [ "$TARGETARCH" != "arm" ]; then \
      git clone --quiet --depth 1 --branch "$MDM_PLUGIN_REF" \
        https://github.com/BlizzHacker/rom-hub-mdm \
        /opt/romarr/bundled-plugins/mdm; \
    fi
# Default source plugins (preinstalled_plugins.DEFAULT_PLUGINS), bundled the
# same way so a new install has them with no network. Keep the refs in step.
RUN if [ "$TARGETARCH" != "arm" ]; then \
      for spec in archive-org:v0.4.1 homebrew:v0.3.0 libretro-content:v0.2.0 \
                  scummvm-freeware:v0.2.0 universal-db:v0.2.0; do \
        git clone --quiet --depth 1 --branch "${spec#*:}" \
          "https://github.com/BlizzHacker/rom-hub-${spec%%:*}" \
          "/opt/romarr/bundled-plugins/${spec%%:*}"; \
      done; \
    fi

# bsdtar reads one RAR volume at a time, so a release split into
# .part1.rar/.r00/.001 volumes (the usual shape on usenet) could never import
# (#224). Alpine has no package that joins split RAR sets -- its 7zip is built
# without RAR -- so install 7-Zip's official static build, which reads RAR4
# and RAR5 and is what romarr/library.py::rar_tools looks for as `7zz`.
# Pinned by version and SHA-256 per architecture; the licence ships with it.
RUN set -eu; \
    case "$TARGETARCH" in \
      amd64) a=x64;   s=22cf82fcc6eb7474f1d5bb4f02c0f8ebd573a718089d6833dc30f8350a3b232e ;; \
      arm64) a=arm64; s=0b71f6cfc34669b3bf69f2347efd3429f77020665003a13b9cdc53b784270421 ;; \
      arm)   a=arm;   s=fcc6744c35f5af3dda0d57de379e885626b2c0fca2ac8a33410eba17d1d94954 ;; \
      *) echo "no 7-Zip build for $TARGETARCH; split RAR sets will not import"; exit 0 ;; \
    esac; \
    f="7z2605-linux-$a.tar.xz"; \
    apk add --no-cache --virtual .sevenzip-fetch xz; \
    wget -q -O "/tmp/$f" "https://github.com/ip7z/7zip/releases/download/26.05/$f"; \
    echo "$s  /tmp/$f" | sha256sum -c -; \
    mkdir /tmp/7z; \
    xz -dc "/tmp/$f" | tar -x -C /tmp/7z 7zzs License.txt; \
    install -m 755 /tmp/7z/7zzs /usr/local/bin/7zz; \
    install -D -m 644 /tmp/7z/License.txt /usr/share/licenses/7zip/License.txt; \
    rm -rf /tmp/7z "/tmp/$f"; \
    apk del .sevenzip-fetch; \
    7zz i | grep -q Rar5

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

# The version this build reports about itself, baked from the release tag by CI.
# An empty value means "unknown", which romarr/version.py turns into a sentinel
# that the update check refuses to compare against latest -- a build that cannot
# say what it is must not advertise somebody else's release number, and must not
# claim to be behind either. See romarr/version.py for the whole chain.
ARG ROMARR_VERSION=""
RUN printf '__version__ = "%s"\n' "${ROMARR_VERSION:-0.0.0+unknown}" \
    > /app/romarr/_version.py

# Diagnostic build provenance. Non-tag builds retain the unknown release
# version while carrying their exact source revision (no fabricated release).
ARG ROMARR_BUILD_KIND="unknown"
ARG ROMARR_SOURCE_REF=""
ARG ROMARR_SOURCE_REVISION=""
ARG ROMARR_SOURCE_REPOSITORY=""
RUN python -c 'import os, pathlib; keys = ("ROMARR_BUILD_KIND", "ROMARR_SOURCE_REF", "ROMARR_SOURCE_REVISION", "ROMARR_SOURCE_REPOSITORY"); values = {k.lower(): os.environ.get(k, "") for k in keys}; pathlib.Path("/app/romarr/_build_info.py").write_text("".join(k + " = " + repr(v) + "\n" for k, v in values.items()), encoding="utf-8")'
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
    UMASK=002 \
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
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.revision="${ROMARR_SOURCE_REVISION}"

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "-m", "romarr"]
