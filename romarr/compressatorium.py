"""Compressatorium client for batch ROM decompression and Switch compression.

Compressatorium (github.com/pacnpal/compressatorium) is the service that does
the actual extraction: CHD, RVZ/WIA/GCZ, Z3DS, NSZ, CSO/ZSO/DAX, WUX, NKit,
and plain 7z/zip archives. ROMarr owns the DAT index and therefore the verdict
-- "is the decompressed file the published dump" is answered here, not by the
extractor.

The API contract below was checked against the service's documented routes
and behaviour at tag v4.5.2. Compressatorium publishes no licence, so ROMarr
uses it only over HTTP and none of its code is copied here:

  * POST   /api/jobs          one job: {file_path, mode, output_dir, ...}
  * POST   /api/jobs/batch    many files, ONE mode per call
  * GET    /api/jobs          list every job
  * GET    /api/jobs/{id}     one job's status
  * DELETE /api/jobs/{id}     cancel a job
  * POST   /api/dat/match     {path} -> {matched, game_name, dat_name} for one
                              file; {matched: false, error} when the match
                              could not be worked out; 503 when matching left
                              a process stuck on unresponsive storage
  * GET    /api/tools         {available: [tool id], unavailable: [tool id]}:
                              a tool whose prerequisites are missing (nsz
                              without the operator's prod.keys) is unavailable
  * GET    /health            unauthenticated liveness, {status, version}

A job is created with a `mode` -- the ConversionMode enum value naming which
tool runs. There is no "decompress whatever this is" mode, so this module
maps file extensions to modes itself and groups a batch by mode, because
/api/jobs/batch takes one mode for every file in the call.

Two modes need more than a suffix to choose:

  * a CHD is decompressed by a different chdman mode per media type, so its
    header is read (`chd_header`) to choose between extractcd, extractdvd,
    extracthd, extractld and extractraw;
  * `romz_extract` takes only a .7z/.zip holding exactly one cartridge ROM
    (OS clutter such as `__MACOSX/` aside). Any other archive -- a zipped
    cue/bin, a multi-file set, a split RAR -- has no Compressatorium mode, and
    `decompress` extracts it with ROMarr's own archive readers instead.

One mode runs the other way. `nsz_compress` turns a Switch .nsp/.xci into
.nsz/.xcz and takes a `compression` value of "<solid|block>:<level>"
(`compress_mode_for`, `submit_job(compression=...)`); `switch_compress` uses
it after a Switch import.

Auth, when the service enables it, accepts a bearer token; the header name
is `Authorization: Bearer <token>` (the service also reads
`X-Compressatorium-Token`, but bearer is the form its own docs use).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import requests

from .chd_header import extract_mode_for_chd
from .library import is_clutter

log = logging.getLogger(__name__)


# --------------------------------------------------------------- config --

@dataclass(frozen=True)
class CompressatoriumConfig:
    """Connection settings for one Compressatorium instance."""

    base_url: str
    api_key: str = ""
    timeout: int = 60
    # Health checks get a short budget so a status page never hangs on a
    # decompression server that is mid-job on a 12GB disc image.
    health_timeout: int = 5


# ------------------------------------------------------------ mode map --

#: Extension -> ConversionMode value for decompression.
#:
#: Checked in order, longest-suffix first, because `.nkit.iso` must win over
#: `.iso` -- and `.iso` is deliberately absent: a plain ISO is not compressed
#: and there is nothing to do.
_MODE_BY_SUFFIX: tuple[tuple[str, str], ...] = (
    (".nkit.iso", "nkit_restore"),
    (".nkit.gcz", "nkit_restore"),
    # 3DS -- the z-prefixed forms are compressed; .cci/.cxi/.cia are not.
    (".z3ds", "z3ds_decompress"),
    (".zcci", "z3ds_decompress"),
    (".zcia", "z3ds_decompress"),
    (".zcxi", "z3ds_decompress"),
    (".z3dsx", "z3ds_decompress"),
    # Switch
    (".nsz", "nsz_decompress"),
    (".xcz", "nsz_decompress"),
    # PSP / PS2 CSO family
    (".cso", "cso_decompress"),
    (".zso", "cso_decompress"),
    (".dax", "cso_decompress"),
    # Wii U
    (".wux", "jwud_decompress"),
    # Dolphin disc images -> ISO
    (".rvz", "dolphin_iso"),
    (".wia", "dolphin_iso"),
    (".gcz", "dolphin_iso"),
    # CHD -> the raw disc image. `extractcd` is the mode that reproduces the
    # source disc (cue+bin for multi-track, iso for single-track) -- the same
    # bytes Redump catalogues.
    (".chd", "extractcd"),
    # General archives
    (".7z", "romz_extract"),
    (".zip", "romz_extract"),
)

#: ConversionMode value -> the id of the Compressatorium tool that runs it,
#: as GET /api/tools names tools. A mode whose tool the service reports
#: unavailable is not submitted: the job could only fail.
TOOL_BY_MODE: dict[str, str] = {
    "nkit_restore": "nkit",
    "z3ds_decompress": "z3ds",
    "nsz_decompress": "nsz",
    "nsz_compress": "nsz",
    "cso_decompress": "cso",
    "jwud_decompress": "jwud",
    "dolphin_iso": "dolphin",
    "extractcd": "chdman",
    "extractdvd": "chdman",
    "extracthd": "chdman",
    "extractld": "chdman",
    "extractraw": "chdman",
    "romz_extract": "romz",
}

#: Extension -> ConversionMode value for compression. Only Switch packages:
#: `nsz_compress` turns .nsp into .nsz and .xci into .xcz. An .nsz/.xcz is
#: already compressed and has no entry.
_COMPRESS_MODE_BY_SUFFIX: dict[str, str] = {
    ".nsp": "nsz_compress",
    ".xci": "nsz_compress",
}

#: Why a tool may be unavailable, for the ones whose reason is known.
UNAVAILABLE_REASON: dict[str, str] = {
    "nsz": "it needs the operator's prod.keys mounted into the container",
}

#: Member suffixes `romz_extract` accepts as "one cartridge ROM". It is a
#: cartridge-archive tool: a disc image, a cue/bin pair or a Switch package
#: inside a .7z is not its job, and the service refuses it.
ROMZ_ROM_SUFFIXES: frozenset[str] = frozenset({
    ".gb", ".gbc", ".gba", ".nds",
    ".nes", ".sfc", ".smc", ".z64", ".n64", ".v64",
    ".sms", ".md", ".gen", ".smd", ".gg",
    ".vb", ".ws", ".wsc", ".ngp", ".ngc", ".lnx",
    ".d64", ".t64", ".prg", ".a26", ".a78",
})

def romz_accepts(members: list[str]) -> bool:
    """Whether `romz_extract` takes an archive holding `members`.

    Exactly one member once clutter is set aside, and it is a cartridge ROM.
    """
    payload = [m for m in members if not is_clutter(m)]
    return (len(payload) == 1
            and PurePosixPath(payload[0].replace("\\", "/")).suffix.lower()
            in ROMZ_ROM_SUFFIXES)


#: Statuses the service reports on a ConversionJob.
QUEUED = "queued"
PROCESSING = "processing"
COMPLETED = "completed"
FAILED = "failed"
CANCELLED = "cancelled"

_TERMINAL = frozenset({COMPLETED, FAILED, CANCELLED})


def mode_for(filename: str) -> str:
    """The decompression mode for a file, or "" when it is not compressed.

    Suffix rather than content: Compressatorium itself picks tools by
    extension, and sniffing a 12GB image to decide which tool to hand it to
    would read the whole file before doing any work.
    """
    lowered = str(filename or "").lower()
    for suffix, mode in _MODE_BY_SUFFIX:
        if lowered.endswith(suffix):
            return mode
    return ""


def compress_mode_for(filename: str) -> str:
    """The compression mode for a file, or "" when there is none.

    Decided by the parsed suffix, so `game.nsp.part` or `nsp-pack.zip` never
    matches. A file that is already compressed (.nsz, .xcz) returns "".
    """
    suffix = PurePosixPath(str(filename or "").replace("\\", "/")).suffix.lower()
    return _COMPRESS_MODE_BY_SUFFIX.get(suffix, "")


def mode_for_file(path: str | Path) -> str:
    """`mode_for`, refined by reading the file where the suffix is not enough.

    A CHD's header names its media, and chdman needs the matching extract
    mode: `extractcd` on a DVD CHD fails. Every other format is decided by
    its suffix alone.
    """
    mode = mode_for(Path(path).name)
    if mode == "extractcd":
        return extract_mode_for_chd(path)
    return mode


# ------------------------------------------------------------- results --

@dataclass(frozen=True)
class JobResult:
    """One ConversionJob as the service reports it.

    `output_path` is a single path -- for a disc image that is the file
    produced; for an archive extraction it is the directory the contents
    were written into. The caller walks it either way.
    """

    job_id: str
    status: str = ""
    file_path: str = ""
    mode: str = ""
    output_path: str = ""
    error: str = ""
    progress: int = 0
    #: A completed job that did less than asked, in the service's words
    #: (a delete-on-verify job that kept its source, for one).
    warning: str = ""
    #: The files the service deleted after verifying the output.
    deleted_paths: tuple[str, ...] = ()
    http_status: int = 0

    @property
    def terminal(self) -> bool:
        return self.status in _TERMINAL


@dataclass(frozen=True)
class MatchResult:
    """What /api/dat/match said about one file."""

    path: str
    matched: bool = False
    game: str = ""
    dat: str = ""
    detail: str = ""
    #: Set when the service could not work out a match at all (a stuck hash
    #: helper, an unreadable embedded hash). That is not "unknown to the
    #: DATs", and the batch must not report it as if it were.
    error: str = ""


# -------------------------------------------------------------- client --

class CompressatoriumClient:
    """Talk to a Compressatorium service.

    Every method returns a structured result rather than raising on a
    server-side failure: a rejected job or an unmatched file is an answer to
    display, not an exception to recover from. Network failures are logged
    and surfaced as empty results so the caller can tell "the server said
    no" from "there was no server".
    """

    name = "Compressatorium"

    def __init__(self, config: CompressatoriumConfig,
                 session: requests.Session | None = None):
        self._config = config
        self._session = session or requests.Session()

    @property
    def configured(self) -> bool:
        return bool(self._config.base_url)

    def _url(self, path: str) -> str:
        return f"{self._config.base_url.rstrip('/')}/{path.lstrip('/')}"

    def _headers(self) -> dict[str, str]:
        if self._config.api_key:
            return {"Authorization": f"Bearer {self._config.api_key}"}
        return {}

    # --------------------------------------------------------- health --

    def reachable(self) -> bool:
        """Whether the service answers at all. /health needs no auth."""
        if not self.configured:
            return False
        try:
            response = self._session.get(
                self._url("/health"), timeout=self._config.health_timeout)
            return response.ok
        except requests.RequestException as err:
            log.warning("compressatorium unreachable: %s", err.__class__.__name__)
            return False

    def health(self) -> dict[str, Any]:
        """The server's health payload for a status page."""
        if not self.configured:
            return {"ok": False, "error": "not configured"}
        try:
            response = self._session.get(
                self._url("/health"), headers=self._headers(),
                timeout=self._config.health_timeout)
            if not response.ok:
                return {"ok": False, "error": f"status {response.status_code}"}
            payload = response.json() if response.content else {}
            if isinstance(payload, dict):
                payload.setdefault("ok", True)
                return payload
            return {"ok": True, "detail": payload}
        except requests.RequestException as err:
            log.warning("compressatorium health check failed: %s", err)
            return {"ok": False, "error": str(err)}
        except ValueError:
            return {"ok": False, "error": "invalid response"}

    def available_tools(self) -> frozenset[str] | None:
        """The tool ids the service says it can run now, or None if unknown.

        None means "do not gate": the route is missing (a release older than
        /api/tools), refused, or unreachable. Gating on a guess would skip
        work the service could have done; submitting anyway lets the service
        give its own answer per job, as it did before this was asked.
        """
        if not self.configured:
            return None
        try:
            response = self._session.get(
                self._url("/api/tools"), headers=self._headers(),
                timeout=self._config.health_timeout)
        except requests.RequestException as err:
            log.warning("compressatorium tool list failed: %s",
                        err.__class__.__name__)
            return None
        if not response.ok:
            return None
        try:
            payload = response.json()
        except ValueError:
            return None
        available = payload.get("available") if isinstance(payload, dict) else None
        if not isinstance(available, list):
            return None
        return frozenset(str(tool) for tool in available)

    # ------------------------------------------------------ job calls --

    def submit_job(self, *, file_path: str, mode: str,
                   output_dir: str | None = None,
                   duplicate_action: str = "skip",
                   delete_on_verify: bool = False,
                   compression: str | None = None) -> JobResult:
        """Queue one conversion job.

        `delete_on_verify` is left False by default even when the caller
        wants originals deleted: ROMarr verifies against its own DAT index
        and deletes only what passed, rather than trusting the service's
        own verify pass. Two independent checks agreeing is the point --
        the service deletes on ITS verification, ROMarr on ROMarr's.
        Compression is the exception: a .nsz has no DAT entry to check, so
        `switch_compress` sets it and relies on the service's round-trip.

        `compression` is sent only when given, as the service reads it per
        mode ("block:18" for `nsz_compress`).
        """
        if not self.configured:
            return JobResult(job_id="", error="not configured")
        if not mode:
            return JobResult(job_id="", error=f"no decompression mode for "
                                              f"{file_path!r}")
        body: dict[str, Any] = {
            "file_path": file_path,
            "mode": mode,
            "duplicate_action": duplicate_action,
            "delete_on_verify": bool(delete_on_verify),
        }
        if output_dir:
            body["output_dir"] = output_dir
        if compression:
            body["compression"] = compression
        try:
            response = self._session.post(
                self._url("/api/jobs"), json=body,
                headers=self._headers(), timeout=self._config.timeout)
        except requests.RequestException as err:
            log.warning("compressatorium submit failed: %s", err)
            return JobResult(job_id="", error=str(err))
        return self._parse_job(response)

    def switch_compression_jobs(self, file_path: str) -> dict[str, str] | None:
        """Return job IDs and states for this exact Switch input."""
        try:
            response = self._session.get(self._url("/api/jobs"),
                                         headers=self._headers(), timeout=self._config.timeout)
            if not response.ok:
                return None
            rows = response.json()
        except (requests.RequestException, ValueError):
            return None
        if not isinstance(rows, list):
            return None
        return {row["id"]: str(row.get("status") or QUEUED) for row in rows
                if isinstance(row, dict) and row.get("file_path") == file_path
                and row.get("mode") == "nsz_compress" and row.get("delete_on_verify") is True
                and isinstance(row.get("id"), str) and row["id"]}

    def job_status(self, job_id: str) -> JobResult:
        """How one job is doing. An unknown job reads status="unknown"."""
        if not job_id:
            return JobResult(job_id="", status="unknown", error="no job id")
        if not self.configured:
            return JobResult(job_id=job_id, status="unknown",
                             error="not configured")
        try:
            response = self._session.get(
                self._url(f"/api/jobs/{job_id}"), headers=self._headers(),
                timeout=self._config.timeout)
        except requests.RequestException as err:
            log.warning("compressatorium status check failed: %s", err)
            return JobResult(job_id=job_id, status="unknown", error=str(err))
        if response.status_code == 404:
            return JobResult(job_id=job_id, status="unknown",
                             error="job not found")
        return self._parse_job(response)

    def cancel_job(self, job_id: str) -> bool:
        """Cancel a job. The service's verb is DELETE on the job route."""
        if not job_id or not self.configured:
            return False
        try:
            response = self._session.delete(
                self._url(f"/api/jobs/{job_id}"), headers=self._headers(),
                timeout=self._config.timeout)
        except requests.RequestException as err:
            log.warning("compressatorium cancel failed: %s", err)
            return False
        if not response.ok:
            log.warning("compressatorium cancel rejected %s: %s",
                        job_id, response.status_code)
            return False
        return True

    def wait_for_jobs(self, job_ids: list[str], *, timeout: int = 3600,
                      poll_interval: int = 5) -> dict[str, JobResult]:
        """Poll every job until terminal or the deadline passes.

        Returns the last known state of each job. A job still running at the
        deadline is returned as-is rather than cancelled -- the service keeps
        working on it and the caller can ask again later, which matters when
        a 12GB disc image outlives the patience of one HTTP request.
        """
        results: dict[str, JobResult] = {}
        pending = list(job_ids)
        deadline = time.monotonic() + timeout
        while pending and time.monotonic() < deadline:
            still: list[str] = []
            for job_id in pending:
                status = self.job_status(job_id)
                results[job_id] = status
                if not status.terminal:
                    still.append(job_id)
            pending = still
            if pending:
                time.sleep(poll_interval)
        return results

    # --------------------------------------------------- DAT matching --

    def match_file(self, path: str) -> MatchResult:
        """Ask the service to match one file against the DATs IT has loaded.

        This is a second opinion alongside ROMarr's own DatIndex, not a
        replacement: Compressatorium has embedded-hash matching for CHD and
        Dolphin formats that a naive file hash cannot answer, which is the
        one thing its DAT store does that ROMarr's does not.
        """
        if not self.configured:
            return MatchResult(path=path, detail="not configured")
        try:
            response = self._session.post(
                self._url("/api/dat/match"), json={"path": path},
                headers=self._headers(), timeout=self._config.timeout)
        except requests.RequestException as err:
            log.warning("compressatorium dat match failed: %s", err)
            return MatchResult(path=path, error=f"service unreachable: "
                                                f"{err.__class__.__name__}")
        if not response.ok:
            return MatchResult(path=path, error=_match_refusal(response))
        try:
            payload = response.json()
        except ValueError:
            return MatchResult(path=path, error="invalid response")
        if not isinstance(payload, dict):
            return MatchResult(path=path, error="unexpected response shape")
        return MatchResult(
            path=path,
            matched=bool(payload.get("matched")),
            game=str(payload.get("game_name") or payload.get("name") or ""),
            dat=str(payload.get("dat_name") or payload.get("dat") or ""),
            detail=str(payload.get("detail") or ""),
            error=str(payload.get("error") or ""),
        )

    # ------------------------------------------------------------ parse --

    def _parse_job(self, response: requests.Response) -> JobResult:
        """A ConversionJob response -> JobResult, including error paths."""
        if not response.ok:
            detail = _truncate(response.text, 200)
            log.warning("compressatorium rejected request: %s %s",
                        response.status_code, detail)
            return JobResult(job_id="", error=f"status {response.status_code}",
                             http_status=response.status_code)
        try:
            payload = response.json()
        except ValueError:
            return JobResult(job_id="", error="invalid response")
        if not isinstance(payload, dict):
            return JobResult(job_id="", error="unexpected response shape")
        job_id = str(payload.get("id") or payload.get("job_id") or "")
        deleted = payload.get("deleted_paths")
        if not isinstance(deleted, list):
            deleted = []
        if not job_id:
            return JobResult(job_id="", error="server returned no job id")
        return JobResult(
            job_id=job_id,
            status=str(payload.get("status") or QUEUED),
            file_path=str(payload.get("file_path") or ""),
            mode=str(payload.get("mode") or ""),
            output_path=str(payload.get("output_path") or ""),
            error=str(payload.get("error_message") or ""),
            progress=int(payload.get("progress") or 0),
            warning=str(payload.get("warning") or ""),
            deleted_paths=tuple(str(p) for p in deleted if p),
        )


# --------------------------------------------------------------- helpers --

def _match_refusal(response: requests.Response) -> str:
    """Why /api/dat/match did not answer, in the service's words when it gave any.

    403 means the path is outside the volumes mounted into the service -- the
    usual cause is ROMarr and Compressatorium mounting the library at
    different paths -- and 503 means a hash helper is stuck on storage that
    stopped answering. Both are reasons to fix something, not verdicts.
    """
    detail = ""
    try:
        payload = response.json()
        if isinstance(payload, dict):
            detail = str(payload.get("detail") or "")
    except ValueError:
        detail = ""
    if response.status_code == 403:
        detail = detail or "path is outside the service's volumes"
    return (f"status {response.status_code}: {_truncate(detail, 200)}"
            if detail else f"status {response.status_code}")


def _truncate(text: str, limit: int) -> str:
    """A response body, shortened so a log line stays readable."""
    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "..."
