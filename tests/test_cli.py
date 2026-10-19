import json

import pytest

from pmcfetch.cli import _build_limiter, _build_parser, _resolve_namespace, main
from pmcfetch.ratelimit import MinIntervalLimiter, NullLimiter

CORPUS = {
    "1": {"metadata": {"namespace": "cardiology"}},
    "2": {"metadata": {"namespace": "cardiology"}},
    "3": {"metadata": {"namespace": "virology"}},
}


@pytest.fixture
def corpus_file(tmp_path):
    p = tmp_path / "corpus.json"
    p.write_text(json.dumps(CORPUS), encoding="utf-8")
    return p


def test_namespaces_command_lists_counts(corpus_file, capsys):
    rc = main(["namespaces", "--corpus", str(corpus_file), "--json"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {"cardiology": 2, "virology": 1}


def test_dry_run_makes_no_requests_and_exits_clean(corpus_file, tmp_path, capsys):
    rc = main(
        [
            "download",
            "--corpus",
            str(corpus_file),
            "--out",
            str(tmp_path / "pdfs"),
            "--namespace",
            "cardiology",
            "--dry-run",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "namespace=cardiology n=2" in out
    assert "would fetch PMC1" in out
    assert not (tmp_path / "pdfs").exists()


def _args(**kw):
    base = {"namespace": None, "task_id": None, "namespaces": ""}
    base.update(kw)
    return type("A", (), base)()


def test_explicit_namespace_wins_over_task_id():
    assert _resolve_namespace(_args(namespace="virology", task_id=1), ["a", "b"]) == "virology"


def test_task_id_indexes_the_namespace_list():
    args = _args(task_id=2, namespaces="general,cardiology,immunology")
    assert _resolve_namespace(args, []) == "cardiology"


def test_task_id_falls_back_to_corpus_namespaces():
    assert _resolve_namespace(_args(task_id=1), ["virology", "cardiology"]) == "virology"


def test_sge_task_id_env_is_used_when_flag_absent(monkeypatch):
    monkeypatch.setenv("SGE_TASK_ID", "3")
    args = _args(namespaces="a,b,c")
    assert _resolve_namespace(args, []) == "c"


def test_out_of_range_task_id_is_an_error():
    from pmcfetch.errors import PMCFetchError

    with pytest.raises(PMCFetchError, match="out of range"):
        _resolve_namespace(_args(task_id=9, namespaces="a,b"), [])


def test_no_namespace_selector_means_all_records():
    assert _resolve_namespace(_args(), ["a", "b"]) is None


def test_parser_defaults_match_documented_behaviour():
    args = _build_parser().parse_args(["download", "--corpus", "c.json", "--out", "o"])
    assert args.workers == 4
    assert args.rps == 2.0
    assert args.retries == 4
    assert args.flat is False


def test_rps_zero_disables_pacing_even_with_the_default_jitter():
    args = _build_parser().parse_args(["download", "--corpus", "c", "--out", "o", "--rps", "0"])
    assert isinstance(_build_limiter(args.rps, args.jitter), NullLimiter)


def test_positive_rps_paces_with_jitter():
    limiter = _build_limiter(4.0, 0.1)
    assert isinstance(limiter, MinIntervalLimiter)
    assert (limiter.min_interval, limiter.jitter) == (0.25, 0.1)


def test_missing_corpus_is_a_clean_error(tmp_path, capsys):
    assert main(["namespaces", "--corpus", str(tmp_path / "nope.json")]) == 2
    assert "nope.json" in capsys.readouterr().err


def test_malformed_corpus_is_a_clean_error(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert main(["namespaces", "--corpus", str(bad)]) == 2
    assert "not valid JSON" in capsys.readouterr().err
