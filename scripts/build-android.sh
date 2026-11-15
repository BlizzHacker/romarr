#!/usr/bin/env bash
# Build the embedded Android APK and unsigned release App Bundle.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
: "${ANDROID_HOME:?Set ANDROID_HOME to an Android SDK containing platform 36 and build-tools 36.0.0}"
: "${JAVA_HOME:?Set JAVA_HOME to JDK 17}"
: "${ROMARR_BUILD_PYTHON:?Set ROMARR_BUILD_PYTHON to a Python 3.13 executable}"
export PATH="$JAVA_HOME/bin:$PATH"
cd "$root/desktop/android"
if (($# == 0)); then set -- assembleDebug assembleRelease bundleRelease; fi
exec ./gradlew --no-daemon "$@"
