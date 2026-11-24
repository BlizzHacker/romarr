"""Compress Switch imports to NSZ/XCZ through Compressatorium (#278).

An opt-in step after a Switch import. Each imported .nsp or .xci is submitted
to the configured Compressatorium as one `nsz_compress` job with
`delete_on_verify` on, so the service removes the original only after the
compressed file passes its own round-trip check. An .nsz or .xcz is already
compressed and is left alone.

ROMarr handles no keys. The service reads the operator's prod.keys from its
own container (`SWITCH_KEYS`), and a service without them reports its nsz tool
unavailable, which ends the job here before it is submitted.

Failed or cancelled jobs keep the original. At the wait deadline ROMarr asks
the service to cancel and keeps tracking the job until its outcome is known.

Compressatorium rebuilds an XCI from an XCZ that is rarely byte for byte the
dump, so its round-trip refuses most XCI deletions and those jobs end failed
with the original kept. The outcome says so in the service's words. That is
why the app compresses an .xci only under its own `compress_switch_xci`
setting, which the UI warns about when it is ticked.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .compressatorium import (
    COMPLETED, TOOL_BY_MODE, UNAVAILABLE_REASON,
    CompressatoriumClient, CompressatoriumConfig, JobResult, compress_mode_for,
)

log = logging.getLogger(__name__)

#: Block mode keeps random access in an NSZ and is the only mode an XCZ takes.
DEFAULT_COMPRESSION = "block:18"

#: Output suffix for each input suffix, as the service names its outputs.
OUTPUT_SUFFIX: dict[str, str] = {".nsp": ".nsz", ".xci": ".xcz"}

#: How long one job may run before it is cancelled, and how often it is
#: polled. A 16 GB cartridge at level 18 takes well under an hour on a
#: modest CPU; the budget covers a queue of them behind one another.
DEFAULT_JOB_TIMEOUT = 3 * 3600
DEFAULT_POLL_INTERVAL = 10

#: Outcome statuses.
COMPRESSED = "compressed"   # output written, original removed by the service
KEPT = "kept"               # output written, original still present
FAILED_STATUS = "failed"    # no usable output, original kept
SKIPPED = "skipped"         # nothing to do for this file
PENDING = "pending"         # a submitted job may still finish

_COMPRESSION = re.compile(r"^(solid|block):(\d{1,2})$")


def parse_compression(value: object) -> str:
    """The `compression` setting in canonical form, or ValueError.

    "<solid|block>:<level>" with a level from 1 to 22, which is nsz's range.
    Surrounding space and letter case are forgiven; anything else is refused
    rather than clamped, so a typo never reaches the service as a guess.
    """
    text = str(value if value is not None else "").strip().lower()
    match = _COMPRESSION.match(text)
    if not match or not 1 <= int(match.group(2)) <= 22:
        raise ValueError("NSZ compression must be solid:<level> or block:<level> "
                         "with a level from 1 to 22, e.g. block:18")
    return f"{match.group(1)}:{int(match.group(2))}"


def compression_for(path: str | Path, compression: str) -> str:
    """The compression to send for `path`. An XCI always gets block mode.

    The setting is one value for every Switch import, and an XCZ cannot be
    solid, so an .xci keeps the configured level with block mode.
    """
    canonical = parse_compression(compression)
    if Path(path).suffix.lower() == ".xci":
        return "block:" + canonical.split(":", 1)[1]
    return canonical


def wants_compression(path: str | Path) -> bool:
    """Whether `path` is an .nsp or .xci that compression applies to."""
    return bool(compress_mode_for(Path(path).name))


def expected_output(path: str | Path) -> Path:
    """Where the service writes the compressed file when no output_dir is given."""
    source = Path(path)
    return source.with_suffix(OUTPUT_SUFFIX.get(source.suffix.lower(), source.suffix))


@dataclass(frozen=True)
class CompressOutcome:
    """What happened to one imported file."""

    source: str
    status: str
    output: str = ""
    job_id: str = ""
    error: str = ""

    def describe(self) -> str:
        """One line for History."""
        name = Path(self.source).name
        if self.status == COMPRESSED:
            return f"compressed {name} to {self.output}"
        if self.status == KEPT:
            return (f"compressed {name} to {self.output}, original kept: "
                    f"{self.error or 'the service did not remove it'}")
        if self.status == SKIPPED:
            return f"skipped {name}: {self.error}"
        if self.status == PENDING:
            return f"compression of {name} is still being tracked: {self.error}"
        return f"compression of {name} failed, original kept: {self.error}"


def _settle(source: Path, job_id: str, warning: str = "") -> CompressOutcome:
    """Read the expected sibling output after a completed job."""
    output = expected_output(source)
    if output.is_symlink() or not output.is_file():
        return CompressOutcome(str(source), PENDING, job_id=job_id,
                               error=f"job completed but {output.name} is not "
                                     "in the library")
    if source.exists():
        return CompressOutcome(str(source), KEPT, str(output), job_id, warning)
    return CompressOutcome(str(source), COMPRESSED, str(output), job_id)


def _finished(source: Path, job_id: str, status: JobResult | None) -> CompressOutcome | None:
    """Resolve a terminal reply or a completed replacement after a lost reply."""
    output = expected_output(source)
    if not source.exists() and output.is_file() and not output.is_symlink():
        return _settle(source, job_id)
    if status is None or not status.terminal:
        return None
    if status.status == COMPLETED:
        return _settle(source, job_id, status.warning)
    return CompressOutcome(str(source), FAILED_STATUS, job_id=job_id,
                           error=status.error or f"job {status.status}")


def resolve_job_id(path: str, previous_ids: tuple[str, ...], client: CompressatoriumClient) -> str:
    """Find a single matching job created after our submission snapshot."""
    jobs = client.switch_compression_jobs(path)
    new_ids = set(jobs or ()) - set(previous_ids)
    return next(iter(new_ids)) if len(new_ids) == 1 else ""


def reconcile_import(path: str, job_id: str,
                     client: CompressatoriumClient | None) -> CompressOutcome | None:
    """Check a tracked job once, using its sibling output if the reply was lost."""
    status = client.job_status(job_id) if client is not None and job_id else None
    outcome = _finished(Path(path), job_id, status)
    return outcome if outcome is not None and outcome.status != PENDING else None


def compress_import(path: str | Path, *, url: str, api_key: str = "",
                    compression: str = DEFAULT_COMPRESSION,
                    job_timeout: int = DEFAULT_JOB_TIMEOUT,
                    poll_interval: int = DEFAULT_POLL_INTERVAL,
                    client: CompressatoriumClient | None = None,
                    track_job: Callable[[str, tuple[str, ...]], None] | None = None) -> CompressOutcome:
    """Start compression and wait up to the deadline for a confirmed result.

    Record the source before submitting and the ID once returned. The caller
    can then reconcile an unconfirmed job after a timeout or restart.
    """
    source = Path(path)
    mode = compress_mode_for(source.name)
    if not mode:
        return CompressOutcome(str(source), SKIPPED,
                               error="not an .nsp or .xci")
    try:
        sent = compression_for(source, compression)
    except ValueError as err:
        return CompressOutcome(str(source), FAILED_STATUS, error=str(err))
    if client is None:
        if not url:
            return CompressOutcome(str(source), FAILED_STATUS,
                                   error="no Compressatorium URL configured")
        client = CompressatoriumClient(CompressatoriumConfig(
            base_url=url, api_key=api_key))
    if not client.reachable():
        return CompressOutcome(str(source), FAILED_STATUS,
                               error="Compressatorium is not reachable")
    available = client.available_tools()
    tool = TOOL_BY_MODE[mode]
    if available is not None and tool not in available:
        why = UNAVAILABLE_REASON.get(tool, "its prerequisites are missing")
        return CompressOutcome(str(source), FAILED_STATUS,
                               error=f"Compressatorium's {tool} tool is unavailable: {why}")

    output = expected_output(source)
    if output.exists() or output.is_symlink():
        return CompressOutcome(str(source), SKIPPED, error=f"{output.name} already exists")
    previous_ids = ()
    if track_job is not None:
        existing = client.switch_compression_jobs(str(source))
        if existing is None:
            return CompressOutcome(str(source), FAILED_STATUS, error="could not read the service job list")
        if any(not JobResult(job_id, status).terminal for job_id, status in existing.items()):
            return CompressOutcome(str(source), SKIPPED, error="a compression job already exists for this file")
        previous_ids = tuple(existing)
        track_job("", previous_ids)
    submitted = client.submit_job(file_path=str(source), mode=mode,
                                  duplicate_action="skip",
                                  delete_on_verify=True, compression=sent)
    if not submitted.job_id:
        status = FAILED_STATUS if 400 <= submitted.http_status < 500 else PENDING
        return CompressOutcome(str(source), status,
                               error=submitted.error or "submission could not be confirmed")
    job_id = submitted.job_id
    if track_job is not None:
        track_job(job_id, previous_ids)
    log.info("compress: submitted %s as job %s (%s)", source.name, job_id, sent)

    final = client.wait_for_jobs([job_id], timeout=job_timeout,
                                 poll_interval=poll_interval).get(job_id)
    outcome = _finished(source, job_id, final)
    if outcome is not None:
        return outcome

    cancelled = client.cancel_job(job_id)
    outcome = reconcile_import(str(source), job_id, client)
    if outcome is not None:
        return outcome
    reason = ("awaiting cancellation confirmation" if cancelled
              else "cancellation could not be confirmed")
    return CompressOutcome(str(source), PENDING, job_id=job_id,
                           error=f"after {job_timeout}s, {reason}")
