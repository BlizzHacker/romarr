#!/bin/sh
# Create ROMarr's data folder and, on a first install, its settings file.
# An existing romarr.env is the operator's and is never overwritten. The new
# one is written beside it and renamed into place, so a full disk cannot
# leave a half-written file that later installs would then keep.
DATA=/share/Docker/romarr
mkdir -p "$DATA" || exit 1
if [ ! -f "$DATA/romarr.env" ]; then
    tmp="$DATA/.romarr.env.$$"
    if cp "$APKG_PKG_DIR/romarr.env.default" "$tmp" && chmod 600 "$tmp" \
        && mv "$tmp" "$DATA/romarr.env"; then
        :
    else
        rm -f "$tmp"
        echo "ROMarr: could not write $DATA/romarr.env" >&2
        exit 1
    fi
fi
exit 0
