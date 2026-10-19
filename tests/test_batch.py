import threading
import time

import pytest
from conftest import FakeSource, pdf_bytes

from pmcfetch.batch import (
    FailureLog,
    Outcome,
    download_many,
    download_one,
    target_path,
    write_pdf_atomic,
)
from pmcfetch.errors import HTTPStatusError
from pmcfetch.manifest import CorpusRecord


def client_for(results):
    return FakeSource(results)


REC = CorpusRecord(pmcid="PMC10007932", namespace="cardiology")


def test_target_path_uses_namespace_subfolder(tmp_path):
    assert target_path(tmp_path, REC) == tmp_path / "cardiology" / "PMC10007932.pdf"
    assert target_path(tmp_path, REC, by_namespace=False) == tmp_path / "PMC10007932.pdf"


def test_download_one_writes_pdf_and_creates_namespace_dir(tmp_path):
    body = pdf_bytes()
    res = download_one(client_for([body]), REC, tmp_path)
    assert res.outcome is Outcome.DOWNLOADED
    assert res.path.read_bytes() == body
    assert res.path.parent.name == "cardiology"


def test_existing_valid_file_is_skipped_without_a_request(tmp_path):
    dest = target_path(tmp_path, REC)
    dest.parent.mkdir(parents=True)
    dest.write_bytes(pdf_bytes())
    client = client_for([])  # empty queue: any request would raise
    res = download_one(client, REC, tmp_path)
    assert res.outcome is Outcome.SKIPPED
    assert client.calls == []


def test_truncated_leftover_is_redownloaded_not_treated_as_complete(tmp_path):
    """A size>1000 file that is not a PDF must not satisfy the resume check."""
    dest = target_path(tmp_path, REC)
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"<html>Just a moment" + b" " * 2000)
    body = pdf_bytes()
    res = download_one(client_for([body]), REC, tmp_path)
    assert res.outcome is Outcome.DOWNLOADED
    assert dest.read_bytes() == body


def test_overwrite_forces_refetch(tmp_path):
    dest = target_path(tmp_path, REC)
    dest.parent.mkdir(parents=True)
    dest.write_bytes(pdf_bytes(1500))
    new = pdf_bytes(3000)
    res = download_one(client_for([new]), REC, tmp_path, overwrite=True)
    assert res.outcome is Outcome.DOWNLOADED
    assert dest.read_bytes() == new


def test_failure_is_reported_not_raised_and_leaves_no_file(tmp_path):
    res = download_one(client_for([HTTPStatusError(404)]), REC, tmp_path)
    assert res.outcome is Outcome.FAILED
    assert "404" in res.reason
    assert not target_path(tmp_path, REC).exists()


def test_failure_log_line_format(tmp_path):
    log_path = tmp_path / "failed.txt"
    log = FailureLog(log_path, truncate=True)
    download_one(client_for([HTTPStatusError(404)]), REC, tmp_path, failure_log=log)
    text = log_path.read_text(encoding="utf-8")
    assert text.startswith("--- Failed Downloads Log ---")
    assert "PMC10007932 | Reason: HTTPStatusError: HTTP 404" in text


def test_write_pdf_atomic_leaves_no_part_file(tmp_path):
    dest = tmp_path / "a" / "PMC1.pdf"
    n = write_pdf_atomic(dest, pdf_bytes(1200))
    assert n == 1200
    assert dest.exists()
    assert list(dest.parent.glob("*.part")) == []


def test_failed_write_removes_its_part_file(tmp_path, monkeypatch):
    def disk_full(_fd):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("pmcfetch.batch.os.fsync", disk_full)
    res = download_one(client_for([pdf_bytes()]), REC, tmp_path)
    assert res.outcome is Outcome.FAILED
    assert res.reason.startswith("WriteError:")
    assert list(target_path(tmp_path, REC).parent.iterdir()) == []


def test_body_under_min_bytes_is_rejected_so_the_next_run_does_not_refetch(tmp_path):
    res = download_one(client_for([pdf_bytes(500)]), REC, tmp_path, min_pdf_bytes=1000)
    assert res.outcome is Outcome.FAILED
    assert "500 bytes, below the 1000-byte minimum" in res.reason
    assert not target_path(tmp_path, REC).exists()


def test_non_pdf_body_from_any_source_is_rejected(tmp_path):
    """The batch layer validates bodies itself rather than trusting the PdfSource."""
    res = download_one(client_for([b"<html>Just a moment" + b" " * 2000]), REC, tmp_path)
    assert res.outcome is Outcome.FAILED
    assert res.reason == "NotAPDFError: response body is not a PDF"
    assert not target_path(tmp_path, REC).exists()


@pytest.mark.parametrize("namespace", ["../escape", "a/b", "..\\up", "..", ""])
def test_namespace_cannot_escape_the_output_root(tmp_path, namespace):
    dest = target_path(tmp_path, CorpusRecord("PMC1", namespace)).resolve()
    assert dest.parent.parent == tmp_path.resolve()
    assert dest.name == "PMC1.pdf"


def test_download_many_serial_counts_all_outcomes(tmp_path):
    recs = [
        CorpusRecord("PMC1", "ns"),
        CorpusRecord("PMC2", "ns"),
        CorpusRecord("PMC3", "ns"),
    ]
    client = client_for(
        [pdf_bytes(), HTTPStatusError(404), pdf_bytes()]
    )
    report = download_many(recs, tmp_path, client=client, workers=1)
    assert report.as_dict() == {"total": 3, "downloaded": 2, "skipped": 0, "failed": 1}
    assert [r.pmcid for r in report.failures] == ["PMC2"]


def test_download_many_reports_progress_for_every_record(tmp_path):
    recs = [CorpusRecord("PMC1", "ns"), CorpusRecord("PMC2", "ns")]
    client = client_for([pdf_bytes(), pdf_bytes()])
    seen = []
    download_many(
        recs, tmp_path, client=client, workers=1, progress=lambda r, i, t: seen.append((i, t))
    )
    assert seen == [(1, 2), (2, 2)]


def test_unexpected_error_fails_one_record_without_aborting_a_serial_batch(tmp_path):
    log = FailureLog(tmp_path / "failed.txt")
    recs = [CorpusRecord("PMC1", None), CorpusRecord("PMC2", "ns")]  # None namespace: a caller bug
    report = download_many(
        recs, tmp_path, client=client_for([pdf_bytes()]), workers=1, failure_log=log
    )
    assert report.as_dict() == {"total": 2, "downloaded": 1, "skipped": 0, "failed": 1}
    assert "PMC1 | Reason: " in log.path.read_text(encoding="utf-8")


def test_download_many_empty_input_is_a_noop(tmp_path):
    report = download_many([], tmp_path, client=client_for([]))
    assert report.total == 0


def test_download_many_concurrent_handles_mixed_results(tmp_path):
    n = 12
    recs = [CorpusRecord(f"PMC{i}", "ns") for i in range(1, n + 1)]

    class RoundRobinSource:
        """Thread-safe source: every 3rd id 404s."""

        def fetch_pdf(self, pmcid):
            if int(pmcid.removeprefix("PMC")) % 3 == 0:
                raise HTTPStatusError(404)
            return pdf_bytes()

    client = RoundRobinSource()
    report = download_many(recs, tmp_path, client=client, workers=4)
    assert report.total == n
    assert report.downloaded == 8
    assert report.failed == 4
    assert len(list((tmp_path / "ns").glob("*.pdf"))) == 8


def test_interrupt_cancels_queued_downloads_instead_of_draining_them(tmp_path):
    class SlowSource:
        def __init__(self):
            self.calls = 0
            self._lock = threading.Lock()

        def fetch_pdf(self, pmcid):
            with self._lock:
                self.calls += 1
            time.sleep(0.05)
            return pdf_bytes()

    def interrupt(_res, _i, _total):
        raise KeyboardInterrupt

    recs = [CorpusRecord(f"PMC{i}", "ns") for i in range(1, 61)]
    source = SlowSource()
    with pytest.raises(KeyboardInterrupt):
        download_many(recs, tmp_path, client=source, workers=2, progress=interrupt)
    # The finished download plus the couple in flight, not all 60.
    assert source.calls < 10
