import json

import pytest
from conftest import FakeESearch

import pmcfetch.cli as cli
from pmcfetch.batch import BatchReport, DownloadResult, Outcome
from pmcfetch.cli import _build_limiter, _build_parser, _resolve_namespace, main
from pmcfetch.manifest import load_records
from pmcfetch.ratelimit import MinIntervalLimiter, NullLimiter
from pmcfetch.search import PMCSearch

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


def test_dry_run_json_lists_what_would_be_fetched(corpus_file, tmp_path, capsys):
    argv = ["download", "--corpus", str(corpus_file), "--out", str(tmp_path / "pdfs")]
    assert main(argv + ["--namespace", "cardiology", "--dry-run", "--json"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert (plan["total"], plan["dry_run"]) == (2, True)
    assert [a["pmcid"] for a in plan["would_fetch"]] == ["PMC1", "PMC2"]
    assert not (tmp_path / "pdfs").exists()


@pytest.fixture
def fake_downloads(monkeypatch):
    """The CLI's downloader swapped for one that reports progress like the real one."""
    seen = {"records": None}

    def _download_many(records, dest_root, progress=None, **kwargs):
        seen["records"] = list(records)
        seen["dest_root"] = dest_root
        results = [DownloadResult(r.pmcid, Outcome.DOWNLOADED) for r in records]
        for i, res in enumerate(results, 1):
            if progress:
                progress(res, i, len(results))
        return BatchReport(results)

    monkeypatch.setattr(cli, "download_many", _download_many)
    return seen


def test_download_json_keeps_progress_off_stdout(corpus_file, fake_downloads, tmp_path, capsys):
    assert main(["download", "--corpus", str(corpus_file), "--out", str(tmp_path), "--json"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {
        "namespace": "ALL",
        "total": 3,
        "downloaded": 3,
        "skipped": 0,
        "failed": 0,
    }
    assert "[3/3] ok" in captured.err


def test_download_json_with_nothing_to_fetch_still_prints_json(corpus_file, tmp_path, capsys):
    argv = ["download", "--corpus", str(corpus_file), "--out", str(tmp_path), "--namespace", "nephrology"]
    assert main(argv + ["--json"]) == 0
    assert json.loads(capsys.readouterr().out)["total"] == 0


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


@pytest.mark.parametrize("argv", [["download", "--corpus", "c.json", "--out", "o"], ["search", "q"]])
def test_parser_defaults_match_documented_behaviour(argv):
    args = _build_parser().parse_args(argv)
    assert args.workers == 4
    assert args.rps == 0.0  # the S3 bucket is not rate limited, so no pacing unless asked
    assert isinstance(_build_limiter(args.rps, args.jitter), NullLimiter)
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


@pytest.fixture
def search_cli(monkeypatch, fake_downloads):
    """`pmcfetch search` wired to a fake NCBI and a fake downloader; records what each saw."""
    seen = fake_downloads
    seen["search_kwargs"] = None
    fake = FakeESearch([11, 22, 33, 44, 55])

    def _search(**kwargs):
        seen["search_kwargs"] = kwargs
        return PMCSearch(http_get=fake, limiter=NullLimiter(), sleep=lambda s: None)

    monkeypatch.setattr(cli, "PMCSearch", _search)
    monkeypatch.setattr(cli, "_interactive", lambda: False)
    monkeypatch.delenv("NCBI_API_KEY", raising=False)
    monkeypatch.delenv("NCBI_EMAIL", raising=False)
    return seen


def test_search_alone_only_reports_the_count(search_cli, capsys):
    assert main(["search", "burn scar"]) == 0
    out = capsys.readouterr().out
    assert "5 open-access article(s) match 'burn scar'" in out
    assert "--download N" in out
    assert search_cli["records"] is None


def test_search_download_n_fetches_the_first_n(search_cli, tmp_path, capsys):
    rc = main(["search", "burn", "--download", "2", "--out", str(tmp_path), "--json"])
    assert rc == 0
    assert len(search_cli["records"]) == 2
    captured = capsys.readouterr()
    summary = json.loads(captured.out)  # progress lines must not leak into the JSON
    assert (summary["query"], summary["matches"], summary["downloaded"]) == ("burn", 5, 2)
    assert "[2/2] ok" in captured.err


def test_search_download_all_and_save(search_cli, tmp_path):
    saved = tmp_path / "corpus.json"
    args = ["search", "burn", "--download", "all", "--out", str(tmp_path), "--namespace", "burns"]
    assert main(args + ["--save", str(saved)]) == 0
    assert len(search_cli["records"]) == 5
    loaded = load_records(saved)
    assert sorted(r.pmcid for r in loaded.records) == ["PMC11", "PMC22", "PMC33", "PMC44", "PMC55"]
    assert {r.namespace for r in loaded.records} == {"burns"}


def test_search_save_without_download_saves_every_match_and_downloads_none(search_cli, tmp_path, capsys):
    saved = tmp_path / "burns.json"
    assert main(["search", "burn", "--namespace", "burns", "--save", str(saved)]) == 0
    assert search_cli["records"] is None
    loaded = load_records(saved)
    assert sorted(r.pmcid for r in loaded.records) == ["PMC11", "PMC22", "PMC33", "PMC44", "PMC55"]
    assert {r.namespace for r in loaded.records} == {"burns"}
    out = capsys.readouterr().out
    assert "pmcfetch download --corpus" in out and str(saved) in out


def test_search_save_without_download_does_not_ask(search_cli, monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("asked how many to download"))
    assert main(["search", "burn", "--save", str(tmp_path / "ids.json")]) == 0
    assert search_cli["records"] is None
    assert len(load_records(tmp_path / "ids.json").records) == 5


def test_search_save_writes_a_file_even_with_no_matches(search_cli, monkeypatch, tmp_path, capsys):
    empty = FakeESearch([])
    monkeypatch.setattr(
        cli, "PMCSearch", lambda **kw: PMCSearch(http_get=empty, limiter=NullLimiter(), sleep=lambda s: None)
    )
    saved = tmp_path / "ids.json"
    assert main(["search", "nothing", "--save", str(saved), "--json"]) == 0
    info = json.loads(capsys.readouterr().out)
    assert (info["matches"], info["saved"]) == (0, 0)
    assert len(load_records(saved).records) == 0


def test_search_asks_how_many_in_a_terminal(search_cli, monkeypatch, tmp_path):
    answers = iter(["lots", "3"])
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    assert main(["search", "burn", "--out", str(tmp_path)]) == 0
    assert len(search_cli["records"]) == 3


def test_search_prompt_enter_downloads_nothing(search_cli, monkeypatch):
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "")
    assert main(["search", "burn"]) == 0
    assert search_cli["records"] is None


def test_search_reads_ncbi_credentials_from_the_environment(search_cli, monkeypatch):
    monkeypatch.setenv("NCBI_API_KEY", "envkey")
    monkeypatch.setenv("NCBI_EMAIL", "me@example.org")
    main(["search", "burn"])
    assert search_cli["search_kwargs"]["api_key"] == "envkey"
    assert search_cli["search_kwargs"]["email"] == "me@example.org"


@pytest.mark.parametrize("text, expected", [("all", "all"), ("ALL", "all"), ("1,000", 1000), ("0", 0)])
def test_download_amounts(text, expected):
    args = _build_parser().parse_args(["search", "q", "--download", text])
    assert args.download == expected


@pytest.mark.parametrize("text", ["-1", "some", ""])
def test_bad_download_amounts_are_rejected(text):
    with pytest.raises(SystemExit) as exc:
        _build_parser().parse_args(["search", "q", "--download", text])
    assert exc.value.code == 2
