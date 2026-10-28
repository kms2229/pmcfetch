# pmcfetch

Download open-access research papers as PDFs from **PubMed Central**, straight
from the PMC Open Access Subset on AWS, with resume, retries and atomic writes.
Requires Python 3.10+, and nothing outside the standard library.

```bash
pip install pmcfetch
```

## What it does

**1. Download papers by id.** You already know which papers you want: list their
PMC ids in a text file, one per line (like `PMC10007932`).

```bash
pmcfetch download --corpus ids.txt --out pdfs/
```

**2. Search by keyword.** See how many open-access papers match before
downloading anything.

```bash
pmcfetch search "burn scar"
# [pmcfetch] 20,349 open-access article(s) match 'burn scar'
```

**3. Download the matches.** Take all of them, or only as many as you want.

```bash
pmcfetch search "burn scar" --download 50     # the 50 most relevant
pmcfetch search "burn scar" --download all    # every match
```

In a terminal, `pmcfetch search` asks how many to download right after showing
the count, so steps 2 and 3 can happen in one go.

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

Keyword search — check the count first, then take as many as you want:

```python
from pmcfetch import PMCSearch, S3OpenAccessClient, download_many

search = PMCSearch()                    # PMCSearch(api_key=...) for NCBI's higher rate limit
summary = search.summarize("burn scar AND 2020:2025[pdat]")
print(summary.count, summary.warnings)  # 12013 ()

records = search.records(summary.query, limit=100, namespace="burns")   # limit=None for all
with S3OpenAccessClient() as client:
    report = download_many(records, "pdfs/", client=client, workers=16)
```

## CLI

```bash
# how many open-access papers match? (in a terminal, it then asks how many to download)
pmcfetch search "wound healing AND diabetic foot"

# download some or all of them, without the question
pmcfetch search "burn scar" --download 100 --out pdfs/
pmcfetch search "burn scar" --download all --namespace burns

# or save every match now and download later, e.g. in an HPC array job
pmcfetch search "burn scar" --namespace burns --save burns.json
pmcfetch download --corpus burns.json --out pdfs/

# what's in a corpus file?
pmcfetch namespaces --corpus corpus.json

# pull one namespace
pmcfetch download --corpus corpus.json --out pdfs/ --namespace cardiology --workers 16

# dry run, no requests
pmcfetch download --corpus corpus.json --out pdfs/ --limit 20 --dry-run

# retry failures — the log doubles as an input list
pmcfetch download --corpus pdfs/failed.txt --out pdfs/ --flat
```

Exit code is `1` when any article failed, `0` otherwise — so a batch job fails
loudly instead of silently producing a short corpus. Bad arguments or an
unreadable corpus file exit `2`, and Ctrl-C exits `130`. With `--json`, stdout
carries only the JSON summary and progress goes to stderr, so it pipes cleanly.

S3 has no rate limit, so downloads are not paced by default and `--workers` can
go wide; throughput is bound by your bandwidth. `--rps N` caps requests per
second across all workers if you want to go easier on a shared link.

`search` takes PMC's search syntax: field tags (`smith j[author]`), date ranges
(`2020:2025[pdat]`), `AND`/`OR`/`NOT`. It prints how NCBI read the query and warns
about words it dropped. `--sort relevance` (the default) or `--sort newest` picks
which papers come first when you take fewer than all. `--save` writes the chosen
ids to a corpus file that `pmcfetch download` reads; without `--download`, it
saves every match and downloads none.

## Deployment

- **Azure** — VM or Container Instances with Blob persistence, plus cost notes.
  See [`docs/azure.md`](https://github.com/kms2229/pmcfetch/blob/main/docs/azure.md).
- **HPC / SGE** — `--task-id` (defaulting to `$SGE_TASK_ID`) selects a namespace
  by 1-based index. See
  [`examples/sge/download_array.qsub`](https://github.com/kms2229/pmcfetch/blob/main/examples/sge/download_array.qsub).

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
- **Search counts only open-access articles** (`"open access"[filter]`), the
  ones the bucket holds. In a sample of 800 matches, 98% had an article PDF; the
  rest are licensed for XML and text only, and fail with `PDFNotAvailableError`
  like any other run.
- **Search has no 10,000 cap.** NCBI returns at most 9,999 ids per query, so
  larger result sets are collected by splitting the query into PMC id ranges.
  NCBI ranks only the first 9,999 by relevance; the rest follow newest first.
- **Search goes through NCBI E-utilities**, which allow 3 requests per second,
  or 10 with an NCBI API key from your
  [NCBI account settings](https://account.ncbi.nlm.nih.gov/settings/)
  (`--api-key`, or `$NCBI_API_KEY`). Collecting 20,000 ids takes about 15 seconds.
  NCBI's count can drift by a few between calls.

## Tests

```bash
pip install -e '.[dev]'
python -m pytest tests/ -q
```

No network required — the S3 layer is exercised against scripted listings and
responses: multi-version and paginated listings, supplementary-PDF
disambiguation, 429 storms and `Retry-After`, 404s, truncated resumes, and
concurrent and interrupted batches. Search runs against a scripted ESearch that
enforces NCBI's per-query cap, so the id-range splitting is tested too.

## Attribution

Data comes from the
[NIH NLM NCBI PubMed Central Article Datasets](https://registry.opendata.aws/ncbi-pmc/)
on AWS Open Data. If you redistribute anything built from it, acknowledge NLM as
the source and don't imply NLM endorsement — see the
[bucket README](https://pmc-oa-opendata.s3.amazonaws.com/README.txt).
