# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[semantic versioning](https://semver.org/).

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

[0.1.1]: https://github.com/kms2229/pmcfetch/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/kms2229/pmcfetch/releases/tag/v0.1.0
