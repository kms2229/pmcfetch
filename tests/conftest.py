import json
import re
import sys
import urllib.parse
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def pdf_bytes(size: int = 2000) -> bytes:
    """A body that passes the PDF magic + minimum-size checks."""
    head = b"%PDF-1.4\n"
    return head + b"0" * max(0, size - len(head))


class FakeSource:
    """Scripted :class:`pmcfetch.pdf.PdfSource`.

    ``download_one`` needs nothing but ``fetch_pdf``, so the batch layer is
    exercised against this rather than a full network client.
    """

    def __init__(self, results):
        self.queue = list(results)
        self.calls: list[str] = []
        self.closed = False

    def fetch_pdf(self, pmcid: str) -> bytes:
        self.calls.append(pmcid)
        if not self.queue:
            raise AssertionError(f"FakeSource ran out of results at call {len(self.calls)}")
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self) -> None:
        self.closed = True


def relevance_order(uids):
    """The fake's "relevance" ranking: any fixed order that differs from newest first."""
    return sorted(uids, key=lambda u: ((u * 7919) % 1009, u))


class FakeESearch:
    """Scripted NCBI ESearch endpoint over a fixed set of PMC uids.

    Mirrors what pmcfetch relies on: ``lo:hi[uid]`` narrows the set,
    ``sort=relevance`` changes the order, the default order is newest (highest
    uid) first, and one query yields at most ``cap`` ids. *fail_first* items
    are returned (bytes) or raised (exceptions) before any normal response.
    """

    def __init__(self, uids, *, cap=9999, fail_first=()):
        self.uids = sorted(set(uids))
        self.cap = cap
        self.failures = list(fail_first)
        self.calls: list[dict[str, str]] = []

    def __call__(self, url: str, *, timeout: float = 0.0) -> bytes:  # noqa: ARG002
        params = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        self.calls.append(params)
        if self.failures:
            item = self.failures.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        if int(params.get("retstart", 0)) > self.cap - 1 or int(params["retmax"]) > self.cap:
            return json.dumps({"esearchresult": {"ERROR": "retstart/retmax past the cap"}}).encode()
        m = re.search(r" AND (\d+):(\d+)\[uid\]$", params["term"])
        hits = [u for u in self.uids if not m or int(m.group(1)) <= u <= int(m.group(2))]
        hits = relevance_order(hits) if params.get("sort") == "relevance" else sorted(hits, reverse=True)
        ids = hits[: int(params["retmax"])]
        return json.dumps(
            {
                "esearchresult": {
                    "count": str(len(hits)),
                    "idlist": [str(u) for u in ids],
                    "querytranslation": params["term"],
                }
            }
        ).encode()


@pytest.fixture
def fake_pdf() -> bytes:
    return pdf_bytes()
