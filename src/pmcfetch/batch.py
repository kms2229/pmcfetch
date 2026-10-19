"""Concurrent, resumable batch downloads."""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable, Sequence

from pmcfetch.pdf import DEFAULT_MIN_PDF_BYTES, PdfSource, is_pdf
from pmcfetch.errors import NotAPDFError
from pmcfetch.manifest import CorpusRecord


class Outcome(str, Enum):
    DOWNLOADED = "downloaded"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass
class DownloadResult:
    pmcid: str
    outcome: Outcome
    path: Path | None = None
    bytes_written: int = 0
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome in (Outcome.DOWNLOADED, Outcome.SKIPPED)


@dataclass
class BatchReport:
    results: list[DownloadResult] = field(default_factory=list)

    @property
    def downloaded(self) -> int:
        return sum(1 for r in self.results if r.outcome is Outcome.DOWNLOADED)

    @property
    def skipped(self) -> int:
        return sum(1 for r in self.results if r.outcome is Outcome.SKIPPED)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if r.outcome is Outcome.FAILED)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def failures(self) -> list[DownloadResult]:
        return [r for r in self.results if r.outcome is Outcome.FAILED]

    def as_dict(self) -> dict[str, int]:
        return {
            "total": self.total,
            "downloaded": self.downloaded,
            "skipped": self.skipped,
            "failed": self.failed,
        }


class FailureLog:
    """Thread-safe append-only failure log.

    Each line is ``<pmcid> | Reason: <reason>``, which ``load_records`` reads
    back as an id list, so a log doubles as the input for a retry run.
    """

    def __init__(self, path: str | Path | None, *, truncate: bool = False) -> None:
        self.path = Path(path).expanduser() if path else None
        self._lock = threading.Lock()
        if self.path and truncate:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text("--- Failed Downloads Log ---\n", encoding="utf-8")

    def record(self, pmcid: str, reason: str) -> None:
        if not self.path:
            return
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(f"{pmcid} | Reason: {reason}\n")


def _namespace_dir(namespace: str) -> str:
    """*namespace* as a single path component, so corpus data cannot escape the output root."""
    name = namespace.replace("/", "_").replace("\\", "_").replace("\0", "_")
    return name if name.strip(".") else "_"


def target_path(
    dest_root: str | Path,
    record: CorpusRecord,
    *,
    by_namespace: bool = True,
) -> Path:
    """Where *record* should land: ``<root>/<namespace>/PMC<id>.pdf`` by default."""
    root = Path(dest_root).expanduser()
    folder = root / _namespace_dir(record.namespace) if by_namespace else root
    return folder / f"{record.pmcid}.pdf"


def _already_complete(path: Path, *, min_bytes: int, verify_magic: bool) -> bool:
    """True when *path* is a usable PDF already on disk."""
    try:
        if not path.is_file() or path.stat().st_size < max(1, min_bytes):
            return False
    except OSError:
        return False
    if not verify_magic:
        return True
    try:
        with path.open("rb") as fh:
            return is_pdf(fh.read(1024))
    except OSError:
        return False


def write_pdf_atomic(path: Path, data: bytes) -> int:
    """Write *data* via a ``.part`` file + rename.

    Writing straight to the target means an interrupted job (walltime kill,
    node eviction) leaves a truncated file that a size-based resume check then
    treats as complete forever. A write that fails removes its ``.part`` file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # Unique per writer, so two threads handed the same record cannot interleave.
    tmp = path.with_name(f"{path.name}.{os.getpid()}-{threading.get_ident()}.part")
    try:
        with tmp.open("wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return len(data)


def _check_body(data: bytes, pmcid: str, min_bytes: int) -> None:
    """Hold a download to the resume check's standard, so it is skipped next run."""
    if not is_pdf(data):
        raise NotAPDFError(pmcid=pmcid, head=data[:64])
    if len(data) < min_bytes:
        raise NotAPDFError(
            f"response body is {len(data)} bytes, below the {min_bytes}-byte minimum",
            pmcid=pmcid,
            head=data[:64],
        )


def download_one(
    client: PdfSource,
    record: CorpusRecord,
    dest_root: str | Path,
    *,
    by_namespace: bool = True,
    overwrite: bool = False,
    verify_existing: bool = True,
    min_pdf_bytes: int = DEFAULT_MIN_PDF_BYTES,
    failure_log: FailureLog | None = None,
) -> DownloadResult:
    """Fetch one article to disk. Never raises for expected download failures."""
    path = target_path(dest_root, record, by_namespace=by_namespace)

    if not overwrite and _already_complete(
        path, min_bytes=min_pdf_bytes, verify_magic=verify_existing
    ):
        return DownloadResult(record.pmcid, Outcome.SKIPPED, path=path, reason="already_present")

    def _failed(reason: str) -> DownloadResult:
        if failure_log:
            failure_log.record(record.pmcid, reason)
        return DownloadResult(record.pmcid, Outcome.FAILED, reason=reason)

    try:
        data = client.fetch_pdf(record.pmcid)
        _check_body(data, record.pmcid, min_pdf_bytes)
    except Exception as exc:  # noqa: BLE001 - one bad paper must not kill the batch
        return _failed(f"{exc.__class__.__name__}: {exc}")

    try:
        n = write_pdf_atomic(path, data)
    except OSError as exc:
        return _failed(f"WriteError: {exc}")

    return DownloadResult(record.pmcid, Outcome.DOWNLOADED, path=path, bytes_written=n)


def download_many(
    records: Sequence[CorpusRecord] | Iterable[CorpusRecord],
    dest_root: str | Path,
    *,
    client: PdfSource,
    workers: int = 4,
    by_namespace: bool = True,
    overwrite: bool = False,
    verify_existing: bool = True,
    min_pdf_bytes: int = DEFAULT_MIN_PDF_BYTES,
    failure_log: FailureLog | None = None,
    progress: Callable[[DownloadResult, int, int], None] | None = None,
) -> BatchReport:
    """Download *records* concurrently.

    Politeness is governed by the client's rate limiter, not by ``workers``;
    raising ``workers`` increases parallelism of connection setup and disk I/O
    while the limiter still paces outbound requests.

    *progress* runs on the calling thread as each record finishes. If it raises,
    or the batch is interrupted (Ctrl-C), queued records are cancelled and only
    the downloads already in flight are waited for.
    """
    items = list(records)
    report = BatchReport()
    total = len(items)
    if total == 0:
        return report

    n_workers = max(1, int(workers))

    def _run(rec: CorpusRecord) -> DownloadResult:
        try:
            return download_one(
                client,
                rec,
                dest_root,
                by_namespace=by_namespace,
                overwrite=overwrite,
                verify_existing=verify_existing,
                min_pdf_bytes=min_pdf_bytes,
                failure_log=failure_log,
            )
        except Exception as exc:  # noqa: BLE001 - defensive: one bad record must not kill the batch
            reason = f"{exc.__class__.__name__}: {exc}"
            if failure_log:
                failure_log.record(rec.pmcid, reason)
            return DownloadResult(rec.pmcid, Outcome.FAILED, reason=reason)

    def _collect(i: int, res: DownloadResult) -> None:
        report.results.append(res)
        if progress:
            progress(res, i, total)

    if n_workers == 1:
        for i, rec in enumerate(items, start=1):
            _collect(i, _run(rec))
        return report

    pool = ThreadPoolExecutor(max_workers=n_workers)
    try:
        futures = [pool.submit(_run, rec) for rec in items]
        for i, fut in enumerate(as_completed(futures), start=1):
            _collect(i, fut.result())
    finally:
        # Leaving the pool via ``with`` would run every queued download first.
        pool.shutdown(wait=True, cancel_futures=True)
    return report


__all__ = [
    "Outcome",
    "DownloadResult",
    "BatchReport",
    "FailureLog",
    "target_path",
    "write_pdf_atomic",
    "download_one",
    "download_many",
]
