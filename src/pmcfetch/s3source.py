"""PMC Open Access Subset on AWS S3 — the sanctioned bulk source.

NLM publishes every PMC OA article version in the public, anonymous bucket
``s3://pmc-oa-opendata``. Since the February 2026 restructure the layout is one
prefix per article version::

    PMC10007932.1/
        PMC10007932.1.pdf     <- article PDF (when the licence permits)
        PMC10007932.1.xml     <- JATS XML
        PMC10007932.1.txt     <- plain text
        PMC10007932.1.json    <- metadata
        <publisher files>     <- figures, supplementary PDFs

No rate limiting, no login, and it is the access route PMC sanctions for
systematic retrieval.

Needs no third-party packages: S3 is plain anonymous HTTPS, so this module
uses :mod:`urllib` only.
"""

from __future__ import annotations

import json
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from email.message import Message
from typing import Callable

from pmcfetch import __version__
from pmcfetch.pdf import is_pdf
from pmcfetch.errors import DownloadError, HTTPStatusError, NotAPDFError, TransportError
from pmcfetch.ids import normalize_pmcid
from pmcfetch.ratelimit import MinIntervalLimiter, NullLimiter

OA_BUCKET = "pmc-oa-opendata"
OA_BASE_URL = f"https://{OA_BUCKET}.s3.amazonaws.com"
_S3_NS = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
_USER_AGENT = f"pmcfetch/{__version__} (+https://pypi.org/project/pmcfetch/)"

#: Matches the canonical per-version object name, e.g. ``PMC10007932.1.pdf``.
#: Deliberately anchored so publisher supplementary PDFs sitting in the same
#: prefix (``jgo-14-01-302-coif.pdf``) are never mistaken for the article.
_ARTICLE_OBJECT_RE = re.compile(r"^PMC(\d+)\.(\d+)/PMC\1\.\2\.(pdf|xml|txt|json)$")


class ArticleNotInOASubsetError(DownloadError):
    """No object prefix for this PMCID — not in the Open Access Subset."""

    retryable = False


class PDFNotAvailableError(DownloadError):
    """Article exists but carries no PDF (licence does not permit redistribution)."""

    retryable = False


@dataclass(frozen=True)
class ArticleVersion:
    """One resolved article version and the objects available for it."""

    pmcid: str
    version: int
    keys: dict[str, str]  # extension -> full S3 key

    @property
    def prefix(self) -> str:
        return f"{self.pmcid}.{self.version}"

    def url(self, ext: str) -> str | None:
        key = self.keys.get(ext)
        return f"{OA_BASE_URL}/{key}" if key else None

    @property
    def has_pdf(self) -> bool:
        return "pdf" in self.keys


def _retry_after_seconds(headers: Message | None) -> float | None:
    """``Retry-After`` in seconds; the rarely used HTTP-date form is ignored."""
    raw = headers.get("Retry-After") if headers is not None else None
    try:
        return max(0.0, float(raw)) if raw else None
    except ValueError:
        return None


def _http_get(url: str, *, timeout: float) -> bytes:
    """Anonymous GET returning the body, with typed errors."""
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        raise HTTPStatusError(int(exc.code), retry_after=_retry_after_seconds(exc.headers)) from exc
    except Exception as exc:  # noqa: BLE001 - socket/DNS/TLS
        raise TransportError(f"{exc.__class__.__name__}: {exc}") from exc


def list_article_versions(pmcid: str, *, timeout: float = 60.0, http_get=_http_get) -> list[ArticleVersion]:
    """List every version of *pmcid* present in the bucket, lowest version first."""
    canonical = normalize_pmcid(pmcid)
    by_version: dict[int, dict[str, str]] = {}
    token = ""
    while True:
        # Trailing dot keeps PMC1000 from matching PMC10000000.
        url = f"{OA_BASE_URL}/?list-type=2&prefix={canonical}."
        if token:
            url += f"&continuation-token={urllib.parse.quote(token, safe='')}"
        raw = http_get(url, timeout=timeout)

        try:
            root = ET.fromstring(raw)
        except ET.ParseError as exc:
            raise TransportError(f"malformed S3 listing for {canonical}: {exc}") from exc

        for node in root.findall("s3:Contents/s3:Key", _S3_NS):
            key = (node.text or "").strip()
            m = _ARTICLE_OBJECT_RE.match(key)
            if not m or f"PMC{m.group(1)}" != canonical:
                continue
            by_version.setdefault(int(m.group(2)), {})[m.group(3)] = key

        # S3 pages listings at 1,000 keys. An article with many versions or files
        # exceeds that, and the newest version can sort onto a later page.
        if root.findtext("s3:IsTruncated", default="", namespaces=_S3_NS) != "true":
            break
        token = root.findtext("s3:NextContinuationToken", default="", namespaces=_S3_NS)
        if not token:
            raise TransportError(f"truncated S3 listing for {canonical} has no continuation token")

    return [
        ArticleVersion(pmcid=canonical, version=v, keys=by_version[v])
        for v in sorted(by_version)
    ]


def resolve_latest_version(pmcid: str, *, timeout: float = 60.0, http_get=_http_get) -> ArticleVersion:
    """Highest-numbered version of *pmcid*, or raise :class:`ArticleNotInOASubsetError`."""
    versions = list_article_versions(pmcid, timeout=timeout, http_get=http_get)
    if not versions:
        raise ArticleNotInOASubsetError(
            f"{normalize_pmcid(pmcid)} is not in the PMC Open Access Subset",
            pmcid=normalize_pmcid(pmcid),
        )
    return versions[-1]


class S3OpenAccessClient:
    """Fetch article PDFs from ``s3://pmc-oa-opendata``.

    Implements the :class:`pmcfetch.pdf.PdfSource` contract
    (``fetch_pdf(pmcid) -> bytes``), so it drops straight into ``download_one``
    and ``download_many``.
    """

    def __init__(
        self,
        *,
        timeout: float = 60.0,
        retries: int = 3,
        backoff_base: float = 0.5,
        backoff_cap: float = 15.0,
        limiter: MinIntervalLimiter | NullLimiter | None = None,
        http_get: Callable[..., bytes] = _http_get,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.timeout = float(timeout)
        self.retries = max(0, int(retries))
        self.backoff_base = float(backoff_base)
        self.backoff_cap = float(backoff_cap)
        self.limiter = limiter if limiter is not None else NullLimiter()
        self._http_get = http_get
        self._sleep = sleep

    def _get(self, url: str) -> bytes:
        """GET with retry on 429, 5xx and transport errors."""
        total = self.retries + 1
        for attempt in range(total):
            self.limiter.acquire(sleep=self._sleep)
            try:
                return self._http_get(url, timeout=self.timeout)
            except DownloadError as exc:
                if not exc.retryable or attempt == total - 1:
                    raise
                self._sleep(self._backoff(attempt, getattr(exc, "retry_after", None)))
        raise TransportError(f"exhausted retries for {url}")  # pragma: no cover

    def _backoff(self, attempt: int, retry_after: float | None) -> float:
        """Full-jitter exponential delay, stretched to any ``Retry-After``, capped at ``backoff_cap``."""
        delay = random.uniform(0.0, min(self.backoff_cap, self.backoff_base * (2**attempt)))
        if retry_after is not None:
            delay = max(delay, retry_after)
        return min(delay, self.backoff_cap)

    def metadata(self, pmcid: str) -> dict:
        """Article metadata JSON (doi, pmid, licence, is_retracted, urls…)."""
        version = resolve_latest_version(
            pmcid, timeout=self.timeout, http_get=lambda u, timeout: self._get(u)
        )
        url = version.url("json")
        if not url:
            raise ArticleNotInOASubsetError(
                f"{version.prefix} has no metadata object", pmcid=version.pmcid
            )
        return json.loads(self._get(url).decode("utf-8"))

    def fetch_pdf(self, pmcid: str) -> bytes:
        """PDF bytes for the latest version of *pmcid*."""
        version = resolve_latest_version(
            pmcid, timeout=self.timeout, http_get=lambda u, timeout: self._get(u)
        )
        url = version.url("pdf")
        if not url:
            raise PDFNotAvailableError(
                f"{version.prefix} has no PDF in the OA subset "
                "(publisher licence does not permit PDF redistribution); "
                "XML and plain text are still available",
                pmcid=version.pmcid,
            )
        data = self._get(url)
        if not is_pdf(data):
            raise NotAPDFError(pmcid=version.pmcid, head=data[:64])
        return data

    def close(self) -> None:
        """No persistent connection to release; present for API symmetry."""

    def __enter__(self) -> "S3OpenAccessClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


__all__ = [
    "OA_BUCKET",
    "OA_BASE_URL",
    "ArticleVersion",
    "ArticleNotInOASubsetError",
    "PDFNotAvailableError",
    "S3OpenAccessClient",
    "list_article_versions",
    "resolve_latest_version",
]
