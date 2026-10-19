# pmcfetch

Targeted PDF retrieval from the **PMC Open Access Subset**, with resume, retries,
atomic writes, and namespace-aware batch layout. Zero dependencies.

```bash
pip install pmcfetch
pmcfetch download --corpus corpus.json --out pdfs/ --workers 16 --rps 0
```

Requires Python 3.10+. Nothing outside the standard library.

## Why

NLM publishes every PMC Open Access article version in the public bucket
`s3://pmc-oa-opendata` — anonymous HTTPS, no AWS account, no credentials, no rate
limit, and the access route PMC sanctions for systematic retrieval.

But `aws s3 sync` is all-or-nothing. There's no `--from-id-list`, so pulling the
few thousand articles you care about means pulling all ~8 million. And fetching
them one by one with `aws s3 cp` requires knowing things that aren't obvious:

- **Which version?** Articles live under `PMC10007932.1/`, `.2/`, sometimes `.10/`.
  You can't know without listing first — and the sort must be numeric, not
  lexicographic.
- **Which PDF?** `PMC10007932.1/` also contains `jgo-14-01-302-coif.pdf`,
  `-dss.pdf`, `-prf.pdf`. Glob `*.pdf` and you get the wrong file.
- **Not every article has one.** Some licences permit XML and plain-text
  redistribution but not the PDF.

`pmcfetch` handles all three, then adds the batch machinery you'd otherwise
rewrite: resume, atomic writes, concurrency, pacing, and structured failure logs.

## Python API

```python
from pmcfetch import S3OpenAccessClient, load_records, download_many, FailureLog

records = load_records("corpus.json").for_namespace("cardiology")

with S3OpenAccessClient() as client:
    report = download_many(
        records, "pdfs/",
        client=client,
        workers=16,
        failure_log=FailureLog("failed.txt"),
    )

print(report.as_dict())        # {'total': 3139, 'downloaded': 3104, 'skipped': 0, 'failed': 35}
for r in report.failures:
    print(r.pmcid, r.reason)
```

Single article, plus metadata:

```python
with S3OpenAccessClient() as c:
    meta = c.metadata("10007932")
    # {'pmcid': 'PMC10007932', 'doi': '10.21037/jgo-23-76',
    #  'license_code': 'CC BY-NC-ND', 'is_retracted': False, ...}
    if not meta["is_retracted"]:
        pdf = c.fetch_pdf("10007932")   # accepts 10007932 / pmc10007932 / PMC10007932
```

`license_code` and `is_retracted` come free with every article — filter on licence
terms and drop retracted papers before they reach your corpus.

## CLI

```bash
# what's in a corpus file?
pmcfetch namespaces --corpus corpus.json

# pull one namespace
pmcfetch download --corpus corpus.json --out pdfs/ --namespace cardiology --workers 16 --rps 0

# dry run, no requests
pmcfetch download --corpus corpus.json --out pdfs/ --limit 20 --dry-run

# retry failures — the log doubles as an input list
pmcfetch download --corpus pdfs/failed.txt --out pdfs/ --flat
```

Exit code is `1` when any article failed, `0` otherwise — so a batch job fails
loudly instead of silently producing a short corpus. Bad arguments or an
unreadable corpus file exit `2`, and Ctrl-C exits `130`.

S3 has no rate limit, so `--rps 0` switches pacing off entirely (`--jitter` then
has no effect) and `--workers` can go wide; throughput is bound by your bandwidth.

## Deployment

- **Azure** — VM or Container Instances with Blob persistence, plus cost notes.
  See `docs/azure.md` in the repository.
- **HPC / SGE** — `--task-id` (defaulting to `$SGE_TASK_ID`) selects a namespace
  by 1-based index. See `examples/sge/download_array.qsub` in the repository.

Nothing in the library is platform-specific; those are examples.

## Corpus formats

`load_records()` normalises keys (`10007932`, `pmc10007932`, `PMC10007932`) and
accepts:

| Input | Shape |
|---|---|
| assignment JSON | `{"<id>": {"metadata": {"namespace": "cardiology"}}}` |
| full-text dump | `{"<id>": "<full text>"}` |
| extraction output | `{"pmc10007932": {"sections": [...]}}` |
| plain array | `["PMC1", "2", ...]` |
| `.txt` | newline-delimited ids, including pmcfetch's own failure logs |

Keys that aren't PMC ids land in `LoadResult.skipped` rather than raising, so one
bad key can't abort a 20k-article batch.

## Behaviour worth knowing

- **Resume** checks size *and* `%PDF` magic, so a half-written file from a killed
  job is re-fetched rather than treated as complete. Downloads are held to the
  same check before they are written, so every file on disk is one the next run
  skips.
- **Writes are atomic** — `.part` + `fsync` + rename; a failed write removes its
  `.part`.
- **Retries** cover 429/5xx and transport errors with exponential backoff and full
  jitter, stretched to any `Retry-After` the server sends (no wait exceeds
  `backoff_cap`, 15 s by default). 404s and non-PDF bodies are not retried.
- **Rate limiting** is process-wide, so `--workers` and `--rps` are independent.
- **Ctrl-C stops promptly** — queued articles are cancelled; only those already
  in flight finish.
- **Namespaces become one folder name** — path separators are replaced, so a
  corpus file cannot write outside `--out`.
- **Articles with no PDF** raise `PDFNotAvailableError`; ones outside the subset
  raise `ArticleNotInOASubsetError`. Both land in the failure log with a reason.
- **Any object implementing `fetch_pdf(pmcid) -> bytes`** satisfies the
  `PdfSource` protocol and works with `download_many`.

## Tests

```bash
pip install -e '.[dev]'
python -m pytest tests/ -q
```

No network required — the S3 layer is exercised against scripted listings and
responses: multi-version and paginated listings, supplementary-PDF
disambiguation, 429 storms and `Retry-After`, 404s, truncated resumes, and
concurrent and interrupted batches.

## Attribution

Data comes from the
[NIH NLM NCBI PubMed Central Article Datasets](https://registry.opendata.aws/ncbi-pmc/)
on AWS Open Data. If you redistribute anything built from it, acknowledge NLM as
the source and don't imply NLM endorsement — see the
[bucket README](https://pmc-oa-opendata.s3.amazonaws.com/README.txt).
