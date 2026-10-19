"""Exception hierarchy for pmcfetch."""

from __future__ import annotations


class PMCFetchError(Exception):
    """Base for every pmcfetch failure."""


class InvalidPMCIDError(PMCFetchError, ValueError):
    """A supplied identifier is not a usable PMC id."""


class DownloadError(PMCFetchError):
    """Base for per-article download failures."""

    retryable: bool = False

    def __init__(self, message: str, *, pmcid: str = "") -> None:
        self.pmcid = pmcid
        super().__init__(message)


class HTTPStatusError(DownloadError):
    """Server returned a non-200 status."""

    def __init__(self, status_code: int, *, pmcid: str = "", retry_after: float | None = None) -> None:
        self.status_code = int(status_code)
        # Seconds the server asked us to wait (``Retry-After``), when it said.
        self.retry_after = retry_after
        # 429 and 5xx are worth another attempt; 403/404 are not.
        self.retryable = self.status_code == 429 or 500 <= self.status_code < 600
        super().__init__(f"HTTP {self.status_code}", pmcid=pmcid)


class NotAPDFError(DownloadError):
    """Body came back 200 but is not a usable PDF (error page, truncated object, HTML)."""

    retryable = False

    def __init__(
        self, message: str = "response body is not a PDF", *, pmcid: str = "", head: bytes = b""
    ) -> None:
        self.head = head
        super().__init__(message, pmcid=pmcid)


class TransportError(DownloadError):
    """Network or TLS level failure; usually worth retrying."""

    retryable = True


__all__ = [
    "PMCFetchError",
    "InvalidPMCIDError",
    "DownloadError",
    "HTTPStatusError",
    "NotAPDFError",
    "TransportError",
]
