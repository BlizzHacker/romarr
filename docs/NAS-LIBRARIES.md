# Using a library on a NAS

ROMarr can read a folder library directly, or connect to RomM and read its
catalogue through the API. These are different checks: a successful RomM
connection does not prove that the ROM folder is mounted inside ROMarr.

## Docker Desktop on macOS

1. Mount the NAS share on the Mac first. Open a known platform folder and a
   file in Finder to confirm the share is connected.
2. Bind that folder into the ROMarr container. Set the library path in ROMarr
   to the **container path**, not the Mac's `/Volumes/...` path.
3. Check the files from inside the container. Replace the example name/path:
   `docker exec romarr ls -la /roms`
4. If RomM uses the same files, confirm both containers see the same platform
   folders. Their container paths may differ; each app needs its own correct
   path. Check the container user's read permissions too.

A user reported that switching Docker Desktop file sharing from VirtioFS to
gRPC FUSE made their NAS library visible. That is a reported workaround, not
a universal fix: the available sharing options depend on the Docker Desktop
version and Mac configuration. Check Docker's settings and test with one small
platform folder before scanning the full collection.

## Slow reads and temporary storage errors

The Library page shows how many games ROMarr has read and how long the scan
has been running. You can browse the rows already read while the rest loads.
A folder refresh uses one streaming pass instead of counting the tree and
walking it again for every page. Temporary `BlockingIOError` failures get a
small, bounded retry; repeated failures leave an incomplete/error state rather
than claiming the library is empty or complete.

If progress stops, check the NAS connection and mount inside the container.
Do not delete your library or configuration to restart a scan. When reporting
a problem, include the ROMarr and Docker Desktop versions, Mac architecture,
file-sharing mode, library backend (RomM or folder), approximate file count,
and the full exception traceback from Logs. Remove passwords, API keys and
private addresses before sharing logs.

This improves ROMarr's scan behavior; it does not prove a particular Docker
Desktop filesystem problem has been repaired. A traceback is needed to locate
an error reported by an existing installation.

Docker reference: [bind mounts](https://docs.docker.com/engine/storage/bind-mounts/).
