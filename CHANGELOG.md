# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[semantic versioning](https://semver.org/).

## [0.2.0] - 2026-10-28

### Added

- `pmcfetch search QUERY` shows how many open-access articles match a PMC
  search query, then downloads all of them or the first N. `--download N|all`
  answers without asking; in a terminal it asks. `--sort relevance|newest`
  picks which come first. `--save` writes the chosen ids to a corpus file for
  `pmcfetch download`; without `--download`, it saves every match and
  downloads none.
- `PMCSearch` does the same from Python: `summarize()` and `count()` for the
  match count, `ids()` and `records()` for the matches. Result sets above
  NCBI's 9,999-per-query cap are collected by splitting the query into PMC id
  ranges.
- `save_records()` writes records as a corpus file that `load_records()` reads
  back.

### Changed

- `--rps` now defaults to `0`, so downloads are not paced unless you ask. The
  PMC S3 bucket is not rate limited, and the Python API already defaulted to
  no pacing. `--rps 2` restores the old behaviour.

### Fixed

- `--json` now keeps stdout to the JSON summary alone. Progress lines go to
  stderr, and `--dry-run` and runs with nothing to download print JSON too.

## [0.1.1] - 2026-10-26

### Added

- This changelog, plus `Source` and `Changelog` links on the PyPI page.

No code changes.

## [0.1.0] - 2026-10-20

First release.

- `S3OpenAccessClient` fetches article PDFs from the PMC Open Access Subset
  bucket (`s3://pmc-oa-opendata`) over anonymous HTTPS. It resolves the latest
  article version, picks the article PDF over supplementary PDFs, and reads
  licence and retraction metadata.
- `download_many` and `pmcfetch download` run concurrent batches with resume,
  atomic writes, retries with jittered backoff and `Retry-After`, optional rate
  limiting, per-namespace folders, and a failure log that doubles as a retry
  list.
- `load_records` reads corpus JSON in several shapes, or a plain list of ids.
- `pmcfetch namespaces` summarises a corpus file, and `--task-id` (defaulting to
  `$SGE_TASK_ID`) selects a namespace in HPC array jobs.

[0.2.0]: https://github.com/kms2229/pmcfetch/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/kms2229/pmcfetch/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/kms2229/pmcfetch/releases/tag/v0.1.0
