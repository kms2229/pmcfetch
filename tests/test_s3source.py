import json
from email.message import Message

import pytest

from pmcfetch.errors import HTTPStatusError, TransportError
from pmcfetch.s3source import (
    OA_BASE_URL,
    ArticleNotInOASubsetError,
    PDFNotAvailableError,
    S3OpenAccessClient,
    _retry_after_seconds,
    list_article_versions,
    resolve_latest_version,
)

NS = 'xmlns="http://s3.amazonaws.com/doc/2006-03-01/"'


def listing(*keys: str, next_token: str = "") -> bytes:
    body = "".join(f"<Contents><Key>{k}</Key></Contents>" for k in keys)
    if next_token:
        body += f"<IsTruncated>true</IsTruncated><NextContinuationToken>{next_token}</NextContinuationToken>"
    return f'<?xml version="1.0"?><ListBucketResult {NS}>{body}</ListBucketResult>'.encode()


class FakeHTTP:
    """Maps URL substring -> response bytes (or an Exception to raise)."""

    def __init__(self, routes):
        self.routes = routes
        self.calls: list[str] = []

    def __call__(self, url: str, *, timeout: float = 0.0) -> bytes:  # noqa: ARG002
        self.calls.append(url)
        for needle, value in self.routes.items():
            if needle in url:
                if isinstance(value, Exception):
                    raise value
                return value
        raise HTTPStatusError(404)


PDF = b"%PDF-1.5\n" + b"z" * 3000


# ── listing / version resolution ──────────────────────────────────────────

def test_lists_all_versions_lowest_first():
    http = FakeHTTP({"list-type=2": listing(
        "PMC10007932.1/PMC10007932.1.pdf",
        "PMC10007932.2/PMC10007932.2.pdf",
        "PMC10007932.2/PMC10007932.2.xml",
    )})
    versions = list_article_versions("PMC10007932", http_get=http)
    assert [v.version for v in versions] == [1, 2]
    assert versions[1].keys["xml"].endswith("PMC10007932.2.xml")


def test_resolve_picks_highest_version():
    http = FakeHTTP({"list-type=2": listing(
        "PMC10007932.1/PMC10007932.1.pdf",
        "PMC10007932.10/PMC10007932.10.pdf",
        "PMC10007932.2/PMC10007932.2.pdf",
    )})
    # numeric, not lexicographic: 10 > 2
    assert resolve_latest_version("PMC10007932", http_get=http).version == 10


def test_supplementary_pdfs_are_not_mistaken_for_the_article():
    """Real case: PMC10007932 ships coif/dss/prf/rc PDFs beside the article."""
    http = FakeHTTP({"list-type=2": listing(
        "PMC10007932.1/PMC10007932.1.pdf",
        "PMC10007932.1/jgo-14-01-302-coif.pdf",
        "PMC10007932.1/jgo-14-01-302-dss.pdf",
    )})
    v = resolve_latest_version("PMC10007932", http_get=http)
    assert v.keys["pdf"] == "PMC10007932.1/PMC10007932.1.pdf"
    assert v.url("pdf") == f"{OA_BASE_URL}/PMC10007932.1/PMC10007932.1.pdf"


def test_prefix_is_dot_anchored_so_short_ids_do_not_match_long_ones():
    http = FakeHTTP({"list-type=2": listing("PMC10000000.1/PMC10000000.1.pdf")})
    assert list_article_versions("PMC1000", http_get=http) == []
    assert "prefix=PMC1000." in http.calls[0]


def test_article_absent_from_oa_subset_raises():
    http = FakeHTTP({"list-type=2": listing()})
    with pytest.raises(ArticleNotInOASubsetError):
        resolve_latest_version("PMC999999", http_get=http)


def test_listing_follows_continuation_tokens_across_pages():
    """S3 pages at 1,000 keys, and the newest version can sort onto a later page."""
    http = FakeHTTP({
        # Matched first: the page-2 URL also contains "list-type=2".
        "continuation-token=tok%2B1%2F%3D": listing("PMC1.9/PMC1.9.pdf"),
        "list-type=2": listing("PMC1.1/PMC1.1.pdf", "PMC1.10/PMC1.10.xml", next_token="tok+1/="),
    })
    assert [v.version for v in list_article_versions("PMC1", http_get=http)] == [1, 9, 10]
    assert len(http.calls) == 2


def test_truncated_listing_without_a_token_raises_instead_of_looping():
    page = listing("PMC1.1/PMC1.1.pdf").replace(
        b"</ListBucketResult>", b"<IsTruncated>true</IsTruncated></ListBucketResult>"
    )
    with pytest.raises(TransportError, match="continuation token"):
        list_article_versions("PMC1", http_get=FakeHTTP({"list-type=2": page}))


# ── fetch_pdf ─────────────────────────────────────────────────────────────

def client_with(routes):
    return S3OpenAccessClient(http_get=FakeHTTP(routes), sleep=lambda _s: None, retries=2)


def test_fetch_pdf_returns_bytes():
    c = client_with({
        "list-type=2": listing("PMC10011620.1/PMC10011620.1.pdf"),
        "PMC10011620.1.pdf": PDF,
    })
    assert c.fetch_pdf("10011620") == PDF


def test_xml_only_article_raises_pdf_not_available():
    """Some licences permit XML/text redistribution but not the PDF."""
    c = client_with({"list-type=2": listing(
        "PMC12788873.1/PMC12788873.1.xml",
        "PMC12788873.1/PMC12788873.1.txt",
    )})
    with pytest.raises(PDFNotAvailableError, match="no PDF"):
        c.fetch_pdf("PMC12788873")


def test_transport_error_is_retried_then_propagates():
    http = FakeHTTP({"list-type=2": TransportError("connection reset")})
    c = S3OpenAccessClient(http_get=http, sleep=lambda _s: None, retries=2)
    with pytest.raises(TransportError):
        c.fetch_pdf("PMC1")
    assert len(http.calls) == 3  # 1 initial + 2 retries


def test_backoff_is_full_jitter_under_an_exponential_ceiling(monkeypatch):
    monkeypatch.setattr("pmcfetch.s3source.random.uniform", lambda lo, hi: (lo + hi) / 2)
    sleeps: list[float] = []
    http = FakeHTTP({"list-type=2": TransportError("connection reset")})
    c = S3OpenAccessClient(
        http_get=http, sleep=sleeps.append, retries=4, backoff_base=1.0, backoff_cap=5.0
    )
    with pytest.raises(TransportError):
        c.fetch_pdf("PMC1")
    # Midpoints of uniform(0, min(cap, base * 2**attempt)) for attempts 0..3.
    assert sleeps == [0.5, 1.0, 2.0, 2.5]


def test_retry_after_stretches_the_backoff_up_to_the_cap():
    sleeps: list[float] = []
    http = FakeHTTP({"list-type=2": HTTPStatusError(503, retry_after=7.0)})
    c = S3OpenAccessClient(http_get=http, sleep=sleeps.append, retries=2, backoff_cap=10.0)
    with pytest.raises(HTTPStatusError):
        c.fetch_pdf("PMC1")
    assert sleeps == [7.0, 7.0]

    sleeps.clear()
    http = FakeHTTP({"list-type=2": HTTPStatusError(429, retry_after=600.0)})
    c = S3OpenAccessClient(http_get=http, sleep=sleeps.append, retries=1, backoff_cap=10.0)
    with pytest.raises(HTTPStatusError):
        c.fetch_pdf("PMC1")
    assert sleeps == [10.0]


@pytest.mark.parametrize(
    "value,expected",
    [("7", 7.0), ("0.5", 0.5), ("-3", 0.0), ("Wed, 21 Oct 2026 07:28:00 GMT", None), (None, None)],
)
def test_retry_after_header_parsing(value, expected):
    headers = Message()
    if value is not None:
        headers["Retry-After"] = value
    assert _retry_after_seconds(headers) == expected


def test_404_is_not_retried():
    http = FakeHTTP({})  # everything 404s
    c = S3OpenAccessClient(http_get=http, sleep=lambda _s: None, retries=3)
    with pytest.raises(HTTPStatusError):
        c.fetch_pdf("PMC1")
    assert len(http.calls) == 1


def test_metadata_returns_parsed_json():
    meta = {"pmcid": "PMC10011620", "doi": "10.1000/x", "is_retracted": False}
    c = client_with({
        "list-type=2": listing("PMC10011620.1/PMC10011620.1.json"),
        "PMC10011620.1.json": json.dumps(meta).encode(),
    })
    assert c.metadata("PMC10011620")["doi"] == "10.1000/x"


def test_plugs_into_the_shared_batch_runner(tmp_path):
    """Satisfies the PdfSource contract, so download_one works unchanged."""
    from pmcfetch.batch import Outcome, download_one
    from pmcfetch.manifest import CorpusRecord

    c = client_with({
        "list-type=2": listing("PMC10011620.1/PMC10011620.1.pdf"),
        "PMC10011620.1.pdf": PDF,
    })
    res = download_one(c, CorpusRecord("PMC10011620", "cardiology"), tmp_path)
    assert res.outcome is Outcome.DOWNLOADED
    assert res.path.read_bytes() == PDF


def test_context_manager():
    with client_with({}) as c:
        assert c is not None
