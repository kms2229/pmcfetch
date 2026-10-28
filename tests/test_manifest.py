import json

from pmcfetch.manifest import CorpusRecord, load_records, save_records

ASSIGNMENTS = {
    "10007932": {"metadata": {"namespace": "cardiology"}},
    "10011620": {"metadata": {"namespace": "cardiology"}},
    "10017269": {"metadata": {"namespace": "neurology"}},
    "not-a-pmcid": {"metadata": {"namespace": "neurology"}},
}

FULL_TEXT = {"11273038": "1. Introduction ...", "11270198": "Abstract ..."}

EXTRACTED = {
    "pmc10007932": {"paper_id": "pmc10007932", "sections": []},
    "pmc10011620": {"paper_id": "pmc10011620", "sections": []},
}


def write(tmp_path, name, obj):
    p = tmp_path / name
    p.write_text(json.dumps(obj), encoding="utf-8")
    return p


def test_reads_assignment_shape_with_namespaces(tmp_path):
    loaded = load_records(write(tmp_path, "assignments.json", ASSIGNMENTS))
    assert loaded.namespaces == {
        "cardiology": ["PMC10007932", "PMC10011620"],
        "neurology": ["PMC10017269"],
    }
    assert loaded.skipped == ["not-a-pmcid"]


def test_reads_flat_full_text_shape(tmp_path):
    loaded = load_records(write(tmp_path, "full_text.json", FULL_TEXT))
    assert [r.pmcid for r in loaded.records] == ["PMC11273038", "PMC11270198"]
    assert all(r.namespace == "general" for r in loaded.records)


def test_reads_extracted_corpus_lowercase_keys(tmp_path):
    loaded = load_records(write(tmp_path, "extracted.json", EXTRACTED))
    assert [r.pmcid for r in loaded.records] == ["PMC10007932", "PMC10011620"]


def test_reads_plain_json_array(tmp_path):
    loaded = load_records(write(tmp_path, "ids.json", ["PMC1", "2", "junk"]))
    assert [r.pmcid for r in loaded.records] == ["PMC1", "PMC2"]
    assert loaded.skipped == ["junk"]


def test_reads_its_own_failure_log(tmp_path):
    p = tmp_path / "failed.txt"
    p.write_text(
        "--- Failed Downloads Log ---\n"
        "PMC10007932 | Reason: HTTPStatusError: HTTP 404\n"
        "PMC10011620 | Reason: NotAPDFError: response body is not a PDF\n"
        "\n",
        encoding="utf-8",
    )
    loaded = load_records(p)
    assert [r.pmcid for r in loaded.records] == ["PMC10007932", "PMC10011620"]


def test_duplicate_ids_are_collapsed(tmp_path):
    loaded = load_records(write(tmp_path, "dupes.json", ["PMC1", "pmc1", "1"]))
    assert [r.pmcid for r in loaded.records] == ["PMC1"]


def test_for_namespace_filters_and_none_returns_all(tmp_path):
    loaded = load_records(write(tmp_path, "assignments.json", ASSIGNMENTS))
    assert [r.pmcid for r in loaded.for_namespace("neurology")] == ["PMC10017269"]
    assert len(loaded.for_namespace(None)) == 3


def test_namespace_falls_back_to_top_level_key(tmp_path):
    loaded = load_records(write(tmp_path, "x.json", {"1": {"namespace": "virology"}}))
    assert loaded.records == [
        CorpusRecord(pmcid="PMC1", namespace="virology", source_key="1")
    ]


def test_saved_records_load_back_with_their_namespaces(tmp_path):
    records = [CorpusRecord("PMC2", "burns"), CorpusRecord("PMC1", "burns"), CorpusRecord("PMC3")]
    path = save_records(records, tmp_path / "sub" / "corpus.json")
    loaded = load_records(path)
    assert [(r.pmcid, r.namespace) for r in loaded.records] == [
        ("PMC2", "burns"),
        ("PMC1", "burns"),
        ("PMC3", "general"),
    ]
    assert loaded.skipped == []
