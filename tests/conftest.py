import sys
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


@pytest.fixture
def fake_pdf() -> bytes:
    return pdf_bytes()
