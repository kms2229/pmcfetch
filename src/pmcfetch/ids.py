"""PMC identifier normalisation.

Corpus files key papers inconsistently: bare digits (``"10007932"``), lowercase
(``"pmc10007932"``), or canonical (``"PMC10007932"``). S3 object prefixes use the
canonical form, so everything is normalised on the way in.
"""

from __future__ import annotations

import re

from pmcfetch.errors import InvalidPMCIDError

# PMC ids are sequential integers with no minimum width, so match any digit run.
# Leading zeros are stripped so "PMC0010007932" and "PMC10007932" collapse.
_PMCID_RE = re.compile(r"^(?:pmc)?0*(\d+)$", re.IGNORECASE)


def normalize_pmcid(raw: object) -> str:
    """Return the canonical ``PMC<digits>`` form.

    Raises :class:`InvalidPMCIDError` for anything that is not a PMC id, so a
    malformed corpus key fails loudly instead of producing a 404 at fetch time.
    """
    s = str(raw or "").strip()
    if not s:
        raise InvalidPMCIDError("empty PMC id")
    m = _PMCID_RE.match(s)
    if not m:
        raise InvalidPMCIDError(f"not a PMC id: {s!r}")
    return f"PMC{m.group(1)}"


def try_normalize_pmcid(raw: object) -> str | None:
    """Non-raising variant; returns ``None`` when *raw* is not a PMC id."""
    try:
        return normalize_pmcid(raw)
    except InvalidPMCIDError:
        return None

__all__ = ["normalize_pmcid", "try_normalize_pmcid"]
