"""Targeted PDF retrieval from the PMC Open Access Subset.

NLM publishes every PMC Open Access article version in the public, anonymous
bucket ``s3://pmc-oa-opendata``. No AWS account, no credentials, no rate limit,
and it is the access route PMC sanctions for systematic retrieval.

``aws s3 sync`` is all-or-nothing — there is no way to pull just the articles you
care about. This package fills that gap: give it a list of PMC ids and it
resolves each article's version, picks the article PDF (not the publisher's
supplementary PDFs sitting in the same prefix), and writes the results with
resume, retries, atomic writes, and per-namespace layout. Or start from a
keyword search: :class:`PMCSearch` counts the matching open-access articles
before anything is downloaded.

Zero third-party dependencies.

    from pmcfetch import S3OpenAccessClient, load_records, download_many

    records = load_records("corpus.json").for_namespace("cardiology")
    with S3OpenAccessClient() as client:
        report = download_many(records, "pdfs/", client=client, workers=16)
    print(report.as_dict())
"""

from __future__ import annotations

__version__ = "0.2.0"

from pmcfetch.batch import (
    BatchReport,
    DownloadResult,
    FailureLog,
    Outcome,
    download_many,
    download_one,
    target_path,
    write_pdf_atomic,
)
from pmcfetch.errors import (
    DownloadError,
    HTTPStatusError,
    InvalidPMCIDError,
    NotAPDFError,
    PMCFetchError,
    SearchError,
    TransportError,
)
from pmcfetch.ids import normalize_pmcid, try_normalize_pmcid
from pmcfetch.manifest import CorpusRecord, LoadResult, iter_records, load_records, save_records
from pmcfetch.pdf import DEFAULT_MIN_PDF_BYTES, DEFAULT_TIMEOUT, PdfSource, is_pdf
from pmcfetch.ratelimit import MinIntervalLimiter, NullLimiter
from pmcfetch.s3source import (
    OA_BASE_URL,
    OA_BUCKET,
    ArticleNotInOASubsetError,
    ArticleVersion,
    PDFNotAvailableError,
    S3OpenAccessClient,
    list_article_versions,
    resolve_latest_version,
)
from pmcfetch.search import PMCSearch, SearchSummary

__all__ = [
    "__version__",
    # search
    "PMCSearch",
    "SearchSummary",
    # source
    "S3OpenAccessClient",
    "OA_BUCKET",
    "OA_BASE_URL",
    "ArticleVersion",
    "list_article_versions",
    "resolve_latest_version",
    # batch
    "download_one",
    "download_many",
    "BatchReport",
    "DownloadResult",
    "Outcome",
    "FailureLog",
    "target_path",
    "write_pdf_atomic",
    # manifest
    "load_records",
    "save_records",
    "iter_records",
    "CorpusRecord",
    "LoadResult",
    # ids
    "normalize_pmcid",
    "try_normalize_pmcid",
    # pdf / pacing
    "PdfSource",
    "is_pdf",
    "DEFAULT_MIN_PDF_BYTES",
    "DEFAULT_TIMEOUT",
    "MinIntervalLimiter",
    "NullLimiter",
    # errors
    "PMCFetchError",
    "DownloadError",
    "HTTPStatusError",
    "NotAPDFError",
    "PDFNotAvailableError",
    "ArticleNotInOASubsetError",
    "InvalidPMCIDError",
    "TransportError",
    "SearchError",
]
