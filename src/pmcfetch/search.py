"""Keyword search over the PMC Open Access Subset, via NCBI E-utilities.

ESearch answers the two questions to settle before downloading anything: how
many articles match a query, and which ones. Every query is narrowed to
``"open access"[filter]``, the articles the S3 bucket holds with their PDFs.
Author manuscripts and other PMC articles also match keyword queries, but the
bucket has no PDF for them.

ESearch returns at most 9,999 ids per query (``retstart`` stops at 9,998), so a
larger result set is collected by splitting the query into PMC id ranges
(``lo:hi[uid]``) until each range fits.

NCBI allows 3 requests per second without an API key and 10 with one; the
default limiter stays under whichever applies.
"""

from __future__ import annotations

import json
import random
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Callable

from pmcfetch.errors import DownloadError, HTTPStatusError, SearchError, TransportError
from pmcfetch.ids import normalize_pmcid
from pmcfetch.manifest import DEFAULT_NAMESPACE, CorpusRecord
from pmcfetch.ratelimit import MinIntervalLimiter, NullLimiter
from pmcfetch.s3source import _http_get

ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
OA_FILTER = '"open access"[filter]'

#: ESearch refuses ``retstart`` above 9,998, so one query yields at most 9,999 ids.
MAX_IDS_PER_QUERY = 9999

#: Upper bound for id-range splitting. PMC ids pass 13 million in 2026, so this
#: leaves room for years of growth at the cost of a few empty ranges.
_MAX_UID = 1 << 27

#: ``--sort`` names mapped to ESearch's ``sort`` values; ESearch's default
#: order (the empty value) is most recent first.
SORTS = {"relevance": "relevance", "newest": ""}


@dataclass(frozen=True)
class SearchSummary:
    """How many open-access articles match a query, and how NCBI read it."""

    query: str
    count: int
    #: The query as NCBI expanded it, e.g. with MeSH terms.
    translation: str = ""
    #: Phrases NCBI could not find or ignored. A count can look plausible
    #: while part of the query was dropped, so these are worth showing.
    warnings: tuple[str, ...] = ()


def _warnings(result: dict[str, Any]) -> tuple[str, ...]:
    errors = result.get("errorlist") or {}
    notes = result.get("warninglist") or {}
    out = [f"phrase not found: {p}" for p in errors.get("phrasesnotfound", [])]
    out += [f"unknown field: {f}" for f in errors.get("fieldsnotfound", [])]
    out += [f"phrase ignored: {p}" for p in notes.get("phrasesignored", [])]
    out += [f"quoted phrase not found: {p}" for p in notes.get("quotedphrasesnotfound", [])]
    out += [f"NCBI: {m}" for m in notes.get("outputmessages", []) if m != "No items found."]
    return tuple(out)


def _parse(raw: bytes) -> dict[str, Any]:
    """The ``esearchresult`` object, or a typed error."""
    try:
        doc = json.loads(raw)
    except ValueError as exc:
        # NCBI occasionally answers with an HTML error page; worth another try.
        raise TransportError(f"malformed ESearch response: {raw[:80]!r}") from exc
    result = doc.get("esearchresult") if isinstance(doc, dict) else None
    if not isinstance(result, dict):
        raise SearchError(f"unexpected ESearch response: {str(doc)[:200]}")
    if "ERROR" in result:
        raise SearchError(f"NCBI rejected the search: {result['ERROR']}")
    return result


class PMCSearch:
    """Count and list PMC Open Access articles matching a search query.

    Queries use PMC's search syntax, so field tags and date ranges work:
    ``"burn scar"``, ``wound healing AND 2020:2024[pdat]``, ``smith j[author]``.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        email: str | None = None,
        timeout: float = 60.0,
        retries: int = 3,
        backoff_base: float = 0.5,
        backoff_cap: float = 15.0,
        limiter: MinIntervalLimiter | NullLimiter | None = None,
        http_get: Callable[..., bytes] = _http_get,
        sleep: Callable[[float], None] = time.sleep,
        max_ids_per_query: int = MAX_IDS_PER_QUERY,
    ) -> None:
        self.timeout = float(timeout)
        self.retries = max(0, int(retries))
        self.backoff_base = float(backoff_base)
        self.backoff_cap = float(backoff_cap)
        if limiter is None:
            limiter = MinIntervalLimiter(min_interval=0.1 if api_key else 0.34)
        self.limiter = limiter
        self.max_ids_per_query = max(1, int(max_ids_per_query))
        self._params = {"db": "pmc", "retmode": "json", "tool": "pmcfetch"}
        if api_key:
            self._params["api_key"] = api_key
        if email:
            self._params["email"] = email
        self._http_get = http_get
        self._sleep = sleep

    def _esearch(self, term: str, *, retmax: int, sort: str = "") -> dict[str, Any]:
        """One ESearch call, retried on 429, 5xx, transport errors and garbled bodies."""
        params = {**self._params, "term": term, "retmax": str(retmax)}
        if sort:
            params["sort"] = sort
        url = f"{ESEARCH_URL}?{urllib.parse.urlencode(params)}"
        total = self.retries + 1
        for attempt in range(total):
            self.limiter.acquire(sleep=self._sleep)
            try:
                return _parse(self._http_get(url, timeout=self.timeout))
            except DownloadError as exc:
                if not exc.retryable or attempt == total - 1:
                    hint = ""
                    if isinstance(exc, HTTPStatusError) and exc.status_code == 400:
                        hint = " (an invalid NCBI API key causes this)"
                    raise SearchError(f"NCBI ESearch failed: {exc}{hint}") from exc
                delay = random.uniform(0.0, min(self.backoff_cap, self.backoff_base * (2**attempt)))
                retry_after = getattr(exc, "retry_after", None)
                if retry_after is not None:
                    delay = max(delay, retry_after)
                self._sleep(min(delay, self.backoff_cap))
        raise SearchError(f"exhausted retries for {url}")  # pragma: no cover

    @staticmethod
    def _term(query: str, uid_range: tuple[int, int] | None = None) -> str:
        q = str(query or "").strip()
        if not q:
            raise SearchError("empty search query")
        term = f"({q}) AND {OA_FILTER}"
        if uid_range:
            term += f" AND {uid_range[0]}:{uid_range[1]}[uid]"
        return term

    def summarize(self, query: str) -> SearchSummary:
        """Match count for *query*, without fetching any ids."""
        result = self._esearch(self._term(query), retmax=0)
        return SearchSummary(
            query=query,
            count=int(result.get("count", 0)),
            translation=str(result.get("querytranslation", "")),
            warnings=_warnings(result),
        )

    def count(self, query: str) -> int:
        """Number of open-access articles matching *query*."""
        return self.summarize(query).count

    def ids(
        self,
        query: str,
        *,
        limit: int | None = None,
        sort: str = "relevance",
        progress: Callable[[int, int], None] | None = None,
    ) -> list[str]:
        """Canonical PMC ids matching *query*: the first *limit*, or all when ``None``.

        The first 9,999 follow *sort* (``"relevance"`` or ``"newest"``). NCBI
        ranks no further than that, so beyond it ids come range by range,
        newest first. *progress* is called with ``(collected, wanted)`` after
        each range.
        """
        if sort not in SORTS:
            raise ValueError(f"sort must be one of {sorted(SORTS)}, not {sort!r}")
        if limit is not None and limit <= 0:
            return []
        cap = self.max_ids_per_query

        first = self._esearch(self._term(query), retmax=min(limit or cap, cap), sort=SORTS[sort])
        total = int(first.get("count", 0))
        want = total if limit is None else min(limit, total)
        out: list[str] = []
        seen: set[str] = set()

        def _add(raw_ids: list[str]) -> None:
            for raw in raw_ids:
                pmcid = normalize_pmcid(raw)
                if pmcid not in seen and len(out) < want:
                    seen.add(pmcid)
                    out.append(pmcid)

        _add(first.get("idlist", []))
        if len(out) >= want:
            return out

        # Too many for one query: split by id range, newest (highest) range
        # first, until each range fits. The full range is known to be too big.
        mid = _MAX_UID // 2
        stack = [(1, mid), (mid + 1, _MAX_UID)]
        while stack and len(out) < want:
            lo, hi = stack.pop()
            result = self._esearch(self._term(query, (lo, hi)), retmax=cap)
            n = int(result.get("count", 0))
            if n > cap and lo < hi:
                mid = (lo + hi) // 2
                stack += [(lo, mid), (mid + 1, hi)]
                continue
            _add(result.get("idlist", []))
            if progress and n:
                progress(len(out), want)
        return out

    def records(
        self,
        query: str,
        *,
        limit: int | None = None,
        sort: str = "relevance",
        namespace: str = DEFAULT_NAMESPACE,
        progress: Callable[[int, int], None] | None = None,
    ) -> list[CorpusRecord]:
        """:meth:`ids` as :class:`CorpusRecord` objects, ready for ``download_many``."""
        return [
            CorpusRecord(pmcid=pmcid, namespace=namespace, source_key=pmcid)
            for pmcid in self.ids(query, limit=limit, sort=sort, progress=progress)
        ]


__all__ = [
    "ESEARCH_URL",
    "OA_FILTER",
    "MAX_IDS_PER_QUERY",
    "SORTS",
    "PMCSearch",
    "SearchSummary",
]
