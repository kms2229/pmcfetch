"""Read corpus JSON files into a uniform record stream.

Three JSON shapes are supported, matching what corpus tooling commonly emits:

* assignment map -- ``{"<id>": {"metadata": {"namespace": "cardiology"}}}``
* full-text dump  -- ``{"<id>": "<full text>"}``
* extraction out  -- ``{"pmc10007932": {"paper_id": ..., "sections": [...]}}``

A plain JSON array of ids and a newline-delimited ``.txt`` id list also work.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator

from pmcfetch.ids import try_normalize_pmcid

DEFAULT_NAMESPACE = "general"


@dataclass(frozen=True)
class CorpusRecord:
    """One downloadable article."""

    pmcid: str
    namespace: str = DEFAULT_NAMESPACE
    source_key: str = ""


@dataclass
class LoadResult:
    """Records plus the keys that could not be parsed as PMC ids."""

    records: list[CorpusRecord] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.records)

    @property
    def namespaces(self) -> dict[str, list[str]]:
        """``{namespace: [pmcid, ...]}`` preserving first-seen order."""
        out: dict[str, list[str]] = {}
        for rec in self.records:
            out.setdefault(rec.namespace, []).append(rec.pmcid)
        return out

    def for_namespace(self, namespace: str | None) -> list[CorpusRecord]:
        """Records in *namespace*; all records when *namespace* is falsy."""
        if not namespace:
            return list(self.records)
        want = str(namespace).strip()
        return [r for r in self.records if r.namespace == want]


def _namespace_of(value: Any, default: str) -> str:
    """Pull ``metadata.namespace`` (or a top-level ``namespace``) from a record."""
    if not isinstance(value, dict):
        return default
    meta = value.get("metadata")
    if isinstance(meta, dict):
        ns = str(meta.get("namespace") or "").strip()
        if ns:
            return ns
    ns = str(value.get("namespace") or "").strip()
    return ns or default


def iter_records(data: Any, *, default_namespace: str = DEFAULT_NAMESPACE) -> Iterator[CorpusRecord]:
    """Yield :class:`CorpusRecord` for every recognisable PMC id in *data*."""
    if isinstance(data, dict):
        items: Iterable[tuple[Any, Any]] = data.items()
    elif isinstance(data, list):
        items = ((x, None) for x in data)
    else:
        return

    for key, value in items:
        pmcid = try_normalize_pmcid(key)
        if pmcid is None:
            continue
        yield CorpusRecord(
            pmcid=pmcid,
            namespace=_namespace_of(value, default_namespace),
            source_key=str(key),
        )


def load_records(
    path: str | Path,
    *,
    default_namespace: str = DEFAULT_NAMESPACE,
) -> LoadResult:
    """Load a corpus file into a :class:`LoadResult`.

    ``.txt`` inputs are treated as newline-delimited id lists; anything else is
    parsed as JSON. Unparseable keys are collected in ``skipped`` rather than
    raising, so one bad key does not abort a 20k-paper batch.
    """
    p = Path(path).expanduser()
    if p.suffix.lower() == ".txt":
        return _load_id_list(p, default_namespace=default_namespace)

    data = json.loads(p.read_text(encoding="utf-8"))
    result = LoadResult()
    seen: set[str] = set()

    keys: Iterable[Any]
    if isinstance(data, dict):
        keys = data.keys()
    elif isinstance(data, list):
        keys = data
    else:
        keys = []

    for rec in iter_records(data, default_namespace=default_namespace):
        if rec.pmcid in seen:
            continue
        seen.add(rec.pmcid)
        result.records.append(rec)

    resolved = {r.source_key for r in result.records}
    for key in keys:
        sk = str(key)
        if sk not in resolved and try_normalize_pmcid(sk) is None:
            result.skipped.append(sk)

    return result


def _load_id_list(path: Path, *, default_namespace: str) -> LoadResult:
    result = LoadResult()
    seen: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("---"):
            continue
        # tolerate "PMC123 | Reason: ..." lines, so a failure log works as input
        token = s.split("|", 1)[0].strip()
        pmcid = try_normalize_pmcid(token)
        if pmcid is None:
            result.skipped.append(s)
            continue
        if pmcid in seen:
            continue
        seen.add(pmcid)
        result.records.append(CorpusRecord(pmcid=pmcid, namespace=default_namespace, source_key=s))
    return result


__all__ = [
    "DEFAULT_NAMESPACE",
    "CorpusRecord",
    "LoadResult",
    "iter_records",
    "load_records",
]
