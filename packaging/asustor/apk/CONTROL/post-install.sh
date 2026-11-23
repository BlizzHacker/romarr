#!/bin/sh
# Create ROMarr's data folder and, on a first install, its settings file.
# An existing romarr.env is the operator's and is never overwritten.
DATA=/share/Docker/romarr
mkdir -p "$DATA"
if [ ! -f "$DATA/romarr.env" ]; then
    cp "$APKG_PKG_DIR/romarr.env.default" "$DATA/romarr.env"
    chmod 600 "$DATA/romarr.env"
fi
exit 0
