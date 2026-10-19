import pytest

from pmcfetch.errors import InvalidPMCIDError
from pmcfetch.ids import normalize_pmcid, try_normalize_pmcid


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("PMC10007932", "PMC10007932"),
        ("pmc10007932", "PMC10007932"),
        ("10007932", "PMC10007932"),
        ("  PMC10007932  ", "PMC10007932"),
        ("Pmc11273038", "PMC11273038"),
        ("PMC1", "PMC1"),
        ("PMC0010007932", "PMC10007932"),  # leading zeros collapse
    ],
)
def test_normalize_accepts_every_corpus_key_shape(raw, expected):
    assert normalize_pmcid(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", None, "not-an-id", "PMC", "10.1234/abc", "PMC12ab"])
def test_normalize_rejects_junk(raw):
    with pytest.raises(InvalidPMCIDError):
        normalize_pmcid(raw)


def test_try_normalize_returns_none_instead_of_raising():
    assert try_normalize_pmcid("nope") is None
    assert try_normalize_pmcid("PMC123456") == "PMC123456"

