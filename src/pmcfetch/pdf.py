"""PDF validation and the source contract that batch downloads depend on."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

PDF_MAGIC = b"%PDF"

#: Error pages and truncated bodies are small; a real article is not. Used as
#: the floor for both download validation and the resume check, so a truncated
#: leftover is never mistaken for a complete file, and every file written passes
#: the resume check on the next run.
DEFAULT_MIN_PDF_BYTES = 1000

#: Default per-request timeout, seconds.
DEFAULT_TIMEOUT = 60.0


def is_pdf(data: bytes, *, min_bytes: int = 0) -> bool:
    """True when *data* looks like a PDF payload.

    Checks the magic anywhere in the first 1 KiB rather than only at offset 0,
    because some responses carry a short byte-order mark or stray whitespace
    before ``%PDF``.
    """
    if not data:
        return False
    if min_bytes and len(data) < min_bytes:
        return False
    return data[:4] == PDF_MAGIC or PDF_MAGIC in data[:1024]


@runtime_checkable
class PdfSource(Protocol):
    """Anything that can produce PDF bytes for a PMC id.

    :func:`pmcfetch.batch.download_one` needs nothing else, so alternative
    sources can be dropped in without touching the batch runner.
    """

    def fetch_pdf(self, pmcid: str) -> bytes: ...


__all__ = ["PDF_MAGIC", "DEFAULT_MIN_PDF_BYTES", "DEFAULT_TIMEOUT", "is_pdf", "PdfSource"]
