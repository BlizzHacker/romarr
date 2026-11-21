"""Batch decompression of a compressed ROM library.

The workflow issue #22 asked for: a library that was compressed (with
compressatorium or anything else) can be decompressed, every output validated
against the loaded DATs, and the compressed originals deleted -- but only the
ones that passed validation.

Division of labour:

  * **Compressatorium** does the extraction it has a tool for. It speaks
    CHD, RVZ/WIA/GCZ, Z3DS, NSZ, CSO/ZSO/DAX, WUX, NKit and single-cartridge
    7z/zip, each through the right tool (chdman, dolphin-tool, maxcso,
    nsz...). ROMarr does not reimplement any of that; it submits jobs to a
    running service, after asking it which of its tools are ready.
  * **ROMarr** extracts the archives the service has no mode for: RAR
    archives and split RAR sets (#224), and any .7z/.zip that is not one
    cartridge ROM (a zipped cue/bin, a multi-disc set). `decompress_local`
    does that with the importer's own archive readers, so those run even
    with no service configured.
  * **ROMarr** does the verdict. The DAT index lives here, and "is this the
    published dump" is answered by hash against that index -- the same
    question `dat.lookup` answers on every import. The service's own
    `/api/dat/match` is consulted as a second opinion for the formats whose
    hashes are embedded in the container (CHD, Dolphin images), where a
    plain file hash of the container cannot match a DAT of its contents.

Safety model, because the last step deletes files:

  * Originals are deleted only when `delete_originals=True` is passed
    explicitly AND every output of that original matched a DAT entry. For a
    split RAR set the original is every volume of the set.
  * An output that is `unknown` -- no DAT claims it -- keeps its original.
    No DAT does not mean bad file: homebrew, translations and anything an
    out-of-date DAT has not seen yet all land there, and deleting on
    unknown would destroy exactly the files a DAT cannot protect.
  * A `bad-dump` verdict keeps the original and fails the item loudly.
  * With no DATs loaded at all, nothing is ever deleted, however the flags
    are set. Verification is the gate; without it the gate stays shut.
  * Every step is logged: scan, submit, completion, per-file verdict, and
    each deletion. The log is the audit trail for a 500-file batch.

Dry run is a first-class mode: it scans and reports what would be processed,
and by which engine, without submitting a job or writing a file, so an
operator can see the blast radius before authorising a destructive one.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import decompress_local
from .compressatorium import (
    CANCELLED, COMPLETED, FAILED, TOOL_BY_MODE, UNAVAILABLE_REASON,
    CompressatoriumClient, CompressatoriumConfig, JobResult, mode_for,
    mode_for_file, romz_accepts,
)
from .dat import BAD_DUMP, UNKNOWN, VERIFIED, DatIndex, hash_file
from .library import rar_sets

log = logging.getLogger(__name__)


# Extensions that count as "compressed" for the purposes of batch
# decompression. Kept in sync with compressatorium.mode_for: anything the
# client cannot name a mode for is not scanned, so the two lists agreeing is
# what makes the scan honest about what it can actually do. RAR is not here:
# RAR volumes are found by `library.rar_sets`, which knows all three volume
# namings, and they are always extracted locally.
COMPRESSED_EXTENSIONS: frozenset[str] = frozenset({
    # General-purpose archives
    ".7z", ".zip",
    # Compressed disc images (CHD, Dolphin RVZ/WIA/GCZ, CSO family)
    ".chd", ".rvz", ".wia", ".gcz", ".cso", ".zso", ".dax",
    # Compressed 3DS ROMs -- the z-prefixed forms only. `.cci`/`.cxi`/
    # `.cia` are the decompressed originals and must never be treated as
    # work to do.
    ".z3ds", ".zcxi", ".zcia", ".zcci", ".z3dsx",
    # Compressed Switch ROMs
    ".nsz", ".xcz",
    # Compressed Wii U images
    ".wux",
    # NKit-shrunk GameCube/Wii images (compound extensions -- matched with
    # endswith, never with Path.suffix)
    ".nkit.iso", ".nkit.gcz",
})

#: Engines an item can be planned to.
SERVICE = "compressatorium"
LOCAL = "local"

# How long to wait for the whole batch, and how often to poll. A CHD of a
# dual-layer DVD decompresses in minutes on spinning rust; a hundred of them
# queue behind one another on the service, so the batch budget is generous
# and the poll interval short enough that small jobs are not held up.
DEFAULT_JOB_TIMEOUT = 3 * 3600
DEFAULT_POLL_INTERVAL = 5


@dataclass(frozen=True)
class Work:
    """One unit the scan found: a file, or the first volume of a RAR set."""

    source: Path
    volumes: tuple[Path, ...] = ()


@dataclass
class DecompressItem:
    """One source file in the batch and what happened to it."""

    source: str
    status: str = "pending"
    # pending | skipped | submitted | decompressed | verified | failed |
    # deleted | kept
    job_id: str = ""
    mode: str = ""
    engine: str = ""             # compressatorium | local
    plan_note: str = ""          # why a file is extracted locally
    volumes: list[str] = field(default_factory=list)
    output_files: list[str] = field(default_factory=list)
    dat_match: str = ""          # verified | bad-dump | unknown | ""
    dat_detail: str = ""
    error: str = ""
    deleted: bool = False
    #: (device, inode, size, mtime_ns) of each original as it was before any
    #: work started; deletion only removes a file that still matches.
    identities: dict = field(default_factory=dict, repr=False)
    #: Archive members that were not extracted; the original is then kept.
    skipped_members: list[str] = field(default_factory=list)


@dataclass
class DecompressReport:
    """Summary of a batch run."""

    scanned: int = 0
    submitted: int = 0
    extracted_locally: int = 0
    decompressed: int = 0
    verified: int = 0
    failed: int = 0
    deleted: int = 0
    kept: int = 0
    skipped: int = 0
    items: list[DecompressItem] = field(default_factory=list)
    elapsed_seconds: float = 0.0
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "submitted": self.submitted,
            "extracted_locally": self.extracted_locally,
            "decompressed": self.decompressed,
            "verified": self.verified,
            "failed": self.failed,
            "deleted": self.deleted,
            "kept": self.kept,
            "skipped": self.skipped,
            "elapsed_seconds": round(self.elapsed_seconds, 1),
            "error": self.error,
            "items": [
                {
                    "source": it.source,
                    "status": it.status,
                    "job_id": it.job_id,
                    "mode": it.mode,
                    "engine": it.engine,
                    "plan_note": it.plan_note,
                    "volumes": it.volumes,
                    "output_files": it.output_files,
                    "dat_match": it.dat_match,
                    "dat_detail": it.dat_detail,
                    "error": it.error,
                    "deleted": it.deleted,
                }
                for it in self.items
            ],
        }


# ------------------------------------------------------------------ scan --

def _is_compressed_name(name: str, exts: frozenset[str]) -> bool:
    # Endswith rather than suffix: NKit images carry compound extensions
    # (`game.nkit.iso`) that `Path.suffix` reduces to `.iso`, and `.iso`
    # is NOT compressed -- matching on the suffix alone would either miss
    # every NKit file or, worse, pull in plain disc images. And the client
    # must actually know a mode for it, so the scan and the job submission
    # can never disagree.
    lowered = name.lower()
    return any(lowered.endswith(ext) for ext in exts) and bool(mode_for(lowered))


def scan_work(directory: str | Path, *,
              extensions: frozenset[str] | None = None) -> list[Work]:
    """Walk `directory` and return everything ROMarr knows how to decompress.

    Recursive. A RAR set is one unit, found through its first volume; its
    later volumes are never units of their own, because they can only be
    read through the first. Files with no decompression mode (`.iso`,
    `.n64`...) are not returned.
    """
    exts = extensions if extensions is not None else COMPRESSED_EXTENSIONS
    root = Path(directory)
    if not root.is_dir():
        log.warning("decompress: %s is not a directory", root)
        return []
    files = sorted(p for p in root.rglob("*") if p.is_file()
                   and not _in_staging(p, root))
    sets, later = rar_sets(files)
    work: list[Work] = []
    for entry in files:
        if entry in later:
            continue
        if entry in sets:
            work.append(Work(entry, tuple(sets[entry])))
        elif _is_compressed_name(entry.name, exts):
            work.append(Work(entry))
    work.sort(key=lambda w: w.source.name.lower())
    return work


def _in_staging(path: Path, root: Path) -> bool:
    """Whether `path` sits in a local extraction's unfinished staging folder."""
    return any(part.startswith(decompress_local.STAGING_PREFIX)
               for part in path.relative_to(root).parts[:-1])


# -------------------------------------------------------------- planning --

def plan(work: Work) -> DecompressItem:
    """Which engine decompresses `work`, with which mode, and why.

    A local plan says why in `plan_note`, which a dry run shows.
    """
    item = DecompressItem(source=str(work.source),
                          volumes=[str(v) for v in work.volumes])
    if work.volumes:
        item.engine, item.mode = LOCAL, "extract"
        count = len(work.volumes)
        item.plan_note = (f"split RAR set of {count} volumes" if count > 1
                          else "RAR archive")
        item.plan_note += "; Compressatorium has no RAR mode"
        return item
    mode = mode_for_file(work.source)
    if mode == "romz_extract":
        members = decompress_local.archive_members(work.source)
        if members is not None and not romz_accepts(members):
            item.engine, item.mode = LOCAL, "extract"
            item.plan_note = (f"holds {len(members)} file(s), not one "
                              "cartridge ROM; romz_extract takes only that")
            return item
    item.engine, item.mode = SERVICE, mode
    return item


# ----------------------------------------------------------- verification --

def _output_files(output_path: str) -> list[str]:
    """Every regular file a job produced.

    `output_path` is a file for disc-image conversions (CHD -> cue/bin/iso)
    and a directory for archive extractions (7z -> its contents), so both
    shapes are walked the same way.
    """
    if not output_path:
        return []
    out = Path(output_path)
    if out.is_file():
        return [str(out)]
    if out.is_dir():
        return sorted(str(p) for p in out.rglob("*") if p.is_file())
    return []


def _second_opinion(fpath: str,
                    client: CompressatoriumClient | None) -> tuple[bool, str]:
    """Ask the service's DAT store about one file: (matched, detail)."""
    if client is None:
        return False, ""
    remote = client.match_file(fpath)
    if remote.matched:
        return True, f"{remote.game or 'matched'} ({remote.dat or 'server DAT'})"
    if remote.error:
        return False, f"Compressatorium could not match it: {remote.error}"
    return False, remote.detail


def _verify_outputs(files: list[str], dats: DatIndex,
                    client: CompressatoriumClient | None) -> tuple[str, str]:
    """Check every decompressed file against the DATs.

    Returns (status, detail) where status is "verified", "bad-dump" or
    "unknown". Verified means EVERY output matched a DAT entry. The service's
    own /api/dat/match, when a service is in use, is consulted as a second
    opinion for each file, which is what catches CHD and Dolphin outputs
    whose hashes are embedded in the container rather than being a plain
    file hash. A match the service could not work out stays unknown.
    """
    if not files:
        return UNKNOWN, "no output files produced"
    worst = VERIFIED
    details: list[str] = []
    for fpath in files:
        p = Path(fpath)
        if not p.is_file():
            worst = BAD_DUMP
            details.append(f"output missing: {fpath}")
            continue
        matched = False
        detail = ""
        file_bad = False
        # ROMarr's own index first -- it is local and free.
        try:
            hashes = hash_file(p)
            match = dats.lookup(**hashes)
            if match.status == VERIFIED:
                matched = True
                detail = f"{match.game} ({match.dat})"
            elif match.status == BAD_DUMP:
                file_bad = True
                detail = match.detail or "bad dump"
        except OSError as err:
            worst = BAD_DUMP
            details.append(f"could not hash {p.name}: {err}")
            continue
        if not matched:
            matched, remote_detail = _second_opinion(fpath, client)
            if matched:
                detail = remote_detail
            else:
                detail = detail or remote_detail
        if matched:
            details.append(f"{p.name}: verified {detail}".rstrip())
        elif file_bad:
            worst = BAD_DUMP
            details.append(f"{p.name}: {detail}")
        else:
            if worst == VERIFIED:
                worst = UNKNOWN
            details.append(f"{p.name}: {detail or 'not in any loaded DAT'}")
    # A bad-dump verdict outranks unknown: one corrupt output means the
    # extraction is suspect even if its siblings verified.
    return worst, "; ".join(details)


# ----------------------------------------------------------------- engines --

def _skip(item: DecompressItem, report: DecompressReport, reason: str) -> None:
    item.status = "skipped"
    item.error = reason
    report.skipped += 1
    log.info("decompress: skipped %s: %s", Path(item.source).name, reason)


def _connect(url: str, api_key: str, service_items: list[DecompressItem],
             report: DecompressReport) -> CompressatoriumClient | None:
    """A client for the service, or None with every service item skipped.

    No URL or no answer used to end the whole batch. Now it ends only the
    work that needs the service: the RAR sets and multi-file archives ROMarr
    extracts itself still run.
    """
    if not url:
        if service_items:
            report.error = "no compressatorium URL configured"
        for item in service_items:
            _skip(item, report, "needs Compressatorium: set COMPRESSATORIUM_URL")
        return None
    client = CompressatoriumClient(CompressatoriumConfig(
        base_url=url, api_key=api_key))
    if not client.reachable():
        reason = f"compressatorium at {url} is not reachable"
        # `error` explains skipped work; with nothing for the service, the
        # local extractions are the whole run and it did not fail.
        if service_items:
            report.error = reason
        for item in service_items:
            _skip(item, report, reason)
        return None
    return client


def _gate_on_tools(client: CompressatoriumClient,
                   service_items: list[DecompressItem],
                   report: DecompressReport) -> list[DecompressItem]:
    """Skip the items whose tool the service reports unavailable."""
    available = client.available_tools()
    if available is None:
        return service_items
    ready = []
    for item in service_items:
        tool = TOOL_BY_MODE.get(item.mode, "")
        if tool and tool not in available:
            why = UNAVAILABLE_REASON.get(tool, "its prerequisites are missing")
            _skip(item, report, f"Compressatorium's {tool} tool is unavailable: {why}")
        else:
            ready.append(item)
    return ready


def _submit_jobs(client: CompressatoriumClient, items: list[DecompressItem],
                 output_dir: str | Path | None, report: DecompressReport) -> None:
    """Submit one job per item; the service works on them in the background."""
    # Per-file jobs rather than /api/jobs/batch: the batch route takes ONE
    # mode for every file in the call, and a real library mixes CHD, RVZ,
    # 7z and NSZ in one directory. Grouping by mode to use the batch route
    # would buy one HTTP call per group and cost the per-file error
    # reporting that makes a failed batch diagnosable.
    for item in items:
        result: JobResult = client.submit_job(
            file_path=item.source,
            mode=item.mode,
            output_dir=str(output_dir) if output_dir else None,
            duplicate_action="skip",
        )
        if not result.job_id:
            item.status = "failed"
            item.error = result.error or "submission failed"
            report.failed += 1
            log.warning("decompress: submit failed for %s: %s",
                        Path(item.source).name, item.error)
            continue
        item.status = "submitted"
        item.job_id = result.job_id
        report.submitted += 1
        log.info("decompress: submitted %s as job %s (%s)",
                 Path(item.source).name, result.job_id, item.mode)


def _wait_for_jobs(client: CompressatoriumClient, items: list[DecompressItem],
                   job_timeout: int, poll_interval: int,
                   report: DecompressReport) -> None:
    """Wait for every submitted job and record each outcome."""
    job_ids = [it.job_id for it in items if it.job_id]
    final = client.wait_for_jobs(job_ids, timeout=job_timeout,
                                 poll_interval=poll_interval)
    for item in items:
        if item.status == "submitted":
            _record_job(item, final.get(item.job_id), job_timeout, report)


def _record_job(item: DecompressItem, status: JobResult | None,
                job_timeout: int, report: DecompressReport) -> None:
    if status is None:
        item.status = "failed"
        item.error = "job vanished from the service"
        report.failed += 1
    elif status.status == COMPLETED:
        item.status = "decompressed"
        item.output_files = _output_files(status.output_path)
        report.decompressed += 1
        log.info("decompress: job %s completed -> %d file(s)",
                 item.job_id, len(item.output_files))
    elif status.status == FAILED:
        item.status = "failed"
        item.error = status.error or "job failed"
        report.failed += 1
        log.warning("decompress: job %s failed: %s", item.job_id, item.error)
    else:
        # cancelled, or still running at the deadline
        item.status = "failed"
        item.error = (f"job ended as {status.status!r}"
                      if status.status == CANCELLED else
                      f"still {status.status} after {job_timeout}s")
        report.failed += 1
        log.warning("decompress: job %s: %s", item.job_id, item.error)


def _run_local(items: list[DecompressItem], output_dir: str | Path | None,
               report: DecompressReport) -> None:
    """Extract each item with ROMarr's own archive readers."""
    for item in items:
        source = Path(item.source)
        volumes = [Path(v) for v in item.volumes] or None
        root = Path(output_dir) if output_dir else source.parent
        try:
            result = decompress_local.extract_archive(source, volumes, root)
        except Exception as exc:  # one bad archive must not end the batch
            log.exception("decompress: unexpected error extracting %s", source.name)
            result = decompress_local.LocalExtraction(error=f"unexpected error: {exc}")
        if result.error:
            item.status = "failed"
            item.error = result.error
            report.failed += 1
            log.warning("decompress: could not extract %s: %s",
                        source.name, result.error)
            continue
        item.status = "decompressed"
        item.output_files = result.output_files
        item.skipped_members = list(result.skipped_members)
        report.extracted_locally += 1
        report.decompressed += 1
        log.info("decompress: extracted %s locally -> %d file(s)",
                 source.name, len(result.output_files))


def _verify_all(items: list[DecompressItem], dats: DatIndex | None,
                client: CompressatoriumClient | None,
                report: DecompressReport) -> None:
    """Give every decompressed item its DAT verdict."""
    if dats is None or not dats.dats:
        for item in items:
            if item.status == "decompressed" and item.output_files:
                item.status = "kept"
                item.dat_match = UNKNOWN
                item.dat_detail = ("no DATs loaded -- verification skipped, "
                                   "originals kept")
                report.kept += 1
                log.warning("decompress: %s decompressed but no DATs loaded "
                            "for verification", Path(item.source).name)
        return
    for item in items:
        if item.status != "decompressed":
            continue
        status, detail = _verify_outputs(item.output_files, dats, client)
        item.dat_match = status
        item.dat_detail = detail
        name = Path(item.source).name
        if status == VERIFIED:
            item.status = "verified"
            report.verified += 1
            log.info("decompress: %s verified against DAT", name)
        elif status == BAD_DUMP:
            item.status = "failed"
            item.error = f"DAT verification: {detail}"
            report.failed += 1
            log.warning("decompress: %s is a BAD DUMP: %s", name, detail)
        else:
            # Unknown: extracted fine, but no DAT claims it. Kept, never
            # deleted -- see the module docstring for why.
            item.status = "kept"
            report.kept += 1
            log.info("decompress: %s extracted but unknown to the DATs; "
                     "original kept", name)


def _identity(path: Path):
    try:
        st = os.lstat(path)
    except OSError:
        return None
    return [st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns]


def _originals(item: DecompressItem) -> list[Path]:
    """An item's original files, first volume last.

    A RAR set is found by its first volume, so it goes last: a deletion that
    stops part-way still leaves a set the next batch can find.
    """
    paths = [Path(v) for v in item.volumes] or [Path(item.source)]
    first = Path(item.source)
    return [p for p in paths if p != first] + [p for p in paths if p == first]


def _record_identities(items: list[DecompressItem]) -> None:
    for item in items:
        item.identities = {str(p): _identity(p) for p in _originals(item)}


def _delete_if_unchanged(path: Path, expected) -> str:
    """Delete `path` only if it is still the file that was extracted.

    The file is first renamed aside -- atomic, so a replacement arriving after
    that point gets the name back and is never touched -- and the renamed file
    is the one checked and removed. Returns "" when removed, "missing" when it
    is already gone, otherwise why it was kept.
    """
    if not expected:
        return f"{path.name}: not seen before extraction, kept"
    aside = path.with_name(f".{path.name}.romarr-deleting-{uuid.uuid4().hex[:8]}")
    try:
        os.rename(path, aside)
    except FileNotFoundError:
        return "missing"
    except OSError as err:
        return f"{path.name}: {err}"
    if _identity(aside) != list(expected):
        try:  # put it back, never over a file that took the name meanwhile
            os.link(aside, path)
            os.unlink(aside)
            return f"{path.name}: changed since it was extracted, kept"
        except OSError:
            return f"{path.name}: changed since it was extracted, kept as {aside.name}"
    try:
        os.unlink(aside)
    except OSError as err:
        try:
            os.link(aside, path)
            os.unlink(aside)
        except OSError:
            pass
        return f"{path.name}: {err}"
    return ""


def _delete_originals(items: list[DecompressItem],
                      report: DecompressReport) -> None:
    """Delete each verified item's original: its file, or every RAR volume."""
    for item in items:
        if item.status != "verified" or item.dat_match != VERIFIED:
            continue
        if item.skipped_members:
            item.error = ("original kept: some members could not be extracted safely: "
                          + ", ".join(item.skipped_members[:5]))
            log.warning("decompress: kept %s: %s", Path(item.source).name, item.error)
            continue
        failures = []
        removed = 0
        for path in _originals(item):
            outcome = _delete_if_unchanged(path, item.identities.get(str(path)))
            if outcome == "missing":
                continue
            if outcome:
                failures.append(outcome)
                log.warning("decompress: kept %s", outcome)
                # The rest of the set stays together, first volume included.
                break
            removed += 1
        if failures:
            item.error = "could not delete original: " + "; ".join(failures)
        if removed and not failures:
            item.deleted = True
            item.status = "deleted"
            report.deleted += 1
            log.info("decompress: deleted original %s%s (verified: %s)",
                     Path(item.source).name,
                     f" and its {removed - 1} other volume(s)" if removed > 1 else "",
                     item.dat_detail)


# ---------------------------------------------------------------- the batch --

def run_batch(
    directory: str | Path,
    *,
    compressatorium_url: str = "",
    compressatorium_api_key: str = "",
    dats: DatIndex | None = None,
    delete_originals: bool = False,
    output_dir: str | Path | None = None,
    job_timeout: int = DEFAULT_JOB_TIMEOUT,
    poll_interval: int = DEFAULT_POLL_INTERVAL,
    dry_run: bool = False,
) -> DecompressReport:
    """Decompress every compressed file in `directory`.

    Parameters
    ----------
    directory
        Root to scan for compressed files.
    compressatorium_url
        Base URL of the service. Without it, only the work ROMarr extracts
        itself (RAR sets, multi-file archives) runs; the rest is skipped and
        `error` says why.
    compressatorium_api_key
        Bearer token, when the service has auth enabled.
    dats
        ROMarr's loaded DAT index. With None (or an empty index) nothing is
        ever deleted, whatever delete_originals says -- verification is the
        gate and without DATs the gate stays shut.
    delete_originals
        Delete each compressed original after its outputs verify. False by
        default; the API requires an explicit true in the request body.
    output_dir
        Where outputs are written. Default: alongside each source, which is
        what the service does when the field is omitted.
    job_timeout
        Seconds to wait for the whole batch to reach a terminal state.
    poll_interval
        Seconds between status polls.
    dry_run
        Scan and report without submitting or writing anything.
    """
    report = DecompressReport()
    t0 = time.monotonic()

    work = scan_work(directory)
    report.scanned = len(work)
    log.info("decompress: scanned %s, found %d compressed file(s)",
             directory, len(work))
    items = [plan(w) for w in work]
    report.items = items

    if not items:
        report.elapsed_seconds = time.monotonic() - t0
        return report

    if dry_run:
        for item in items:
            item.status = "skipped"
            if item.engine == LOCAL:
                item.dat_detail = f"dry run -- would extract locally ({item.plan_note})"
            else:
                item.dat_detail = f"dry run -- would run {item.mode} on Compressatorium"
            report.skipped += 1
        report.elapsed_seconds = time.monotonic() - t0
        log.info("decompress: dry run, %d file(s) would be processed", len(items))
        return report

    # The deletion gate. Decided once, here, rather than at each deletion
    # site: deleting an original is only ever justified by a DAT match, and
    # with no index loaded there is nothing that could produce one.
    can_delete = bool(delete_originals) and dats is not None and bool(dats.dats)
    if delete_originals and not can_delete:
        log.warning("decompress: delete_originals requested but no DATs are "
                    "loaded -- originals will be KEPT regardless of outcome")

    if can_delete:
        # Before anything reads them: deletion later removes only these files.
        _record_identities(items)
    service_items = [it for it in items if it.engine == SERVICE]
    local_items = [it for it in items if it.engine == LOCAL]
    client = _connect(compressatorium_url, compressatorium_api_key,
                      service_items, report)
    ready: list[DecompressItem] = []
    if client is not None and service_items:
        ready = _gate_on_tools(client, service_items, report)
        _submit_jobs(client, ready, output_dir, report)
    # The service works through its queue while ROMarr extracts its own share.
    _run_local(local_items, output_dir, report)
    if ready:
        _wait_for_jobs(client, ready, job_timeout, poll_interval, report)

    _verify_all(items, dats, client, report)

    if can_delete:
        _delete_originals(items, report)
    elif any(it.status == "verified" for it in items):
        log.info("decompress: %d file(s) verified but originals kept "
                 "(deletion not authorised)",
                 sum(1 for it in items if it.status == "verified"))

    report.elapsed_seconds = time.monotonic() - t0
    log.info("decompress: batch complete -- %d scanned, %d submitted, "
             "%d extracted locally, %d verified, %d kept, %d failed, "
             "%d deleted in %.1fs",
             report.scanned, report.submitted, report.extracted_locally,
             report.verified, report.kept, report.failed, report.deleted,
             report.elapsed_seconds)
    return report
