"""``pmcfetch`` command line interface."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from pmcfetch import __version__
from pmcfetch.batch import DownloadResult, FailureLog, Outcome, download_many
from pmcfetch.pdf import DEFAULT_MIN_PDF_BYTES, DEFAULT_TIMEOUT
from pmcfetch.errors import PMCFetchError
from pmcfetch.manifest import load_records
from pmcfetch.ratelimit import MinIntervalLimiter, NullLimiter


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pmcfetch",
        description="Download open-access PDFs from the PMC Open Access Subset on AWS.",
    )
    parser.add_argument("--version", action="version", version=f"pmcfetch {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    dl = sub.add_parser("download", help="Download PDFs listed in a corpus file")
    dl.add_argument("--corpus", required=True, help="Corpus JSON (or .txt id list)")
    dl.add_argument("--out", required=True, help="Output root directory")
    dl.add_argument(
        "--namespace",
        default=None,
        help="Only download this namespace (e.g. cardiology)",
    )
    dl.add_argument(
        "--namespaces",
        default="",
        help="Comma-separated namespace order used to resolve --task-id",
    )
    dl.add_argument(
        "--task-id",
        type=int,
        default=None,
        help="1-based array index selecting a namespace (defaults to $SGE_TASK_ID)",
    )
    dl.add_argument("--workers", type=int, default=4, help="Concurrent downloads (default 4)")
    dl.add_argument(
        "--rps",
        type=float,
        default=2.0,
        help=(
            "Max outbound requests per second across all workers. The PMC S3 bucket "
            "is not rate limited, so 0 disables pacing entirely (default 2.0)"
        ),
    )
    dl.add_argument(
        "--jitter",
        type=float,
        default=0.2,
        help="Extra random spacing per request, seconds (only applies with --rps > 0)",
    )
    dl.add_argument("--retries", type=int, default=4, help="Retries per article (default 4)")
    dl.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="Per-request timeout")
    dl.add_argument("--limit", type=int, default=0, help="Stop after N articles (0 = all)")
    dl.add_argument("--min-bytes", type=int, default=DEFAULT_MIN_PDF_BYTES, help="Minimum PDF size")
    dl.add_argument("--overwrite", action="store_true", help="Re-download files already on disk")
    dl.add_argument("--flat", action="store_true", help="Write all PDFs into --out, no subfolders")
    dl.add_argument(
        "--no-verify-existing",
        action="store_true",
        help="Resume on file size alone, skipping the %%PDF magic re-check",
    )
    dl.add_argument("--failed-log", default=None, help="Append failures here")
    dl.add_argument("--json", action="store_true", help="Print the summary as JSON")
    dl.add_argument("--quiet", action="store_true", help="Suppress per-article progress")
    dl.add_argument("--dry-run", action="store_true", help="List what would be fetched, then exit")

    ns = sub.add_parser("namespaces", help="Show namespace counts in a corpus file")
    ns.add_argument("--corpus", required=True)
    ns.add_argument("--json", action="store_true")

    return parser


def _resolve_namespace(args: argparse.Namespace, available: list[str]) -> str | None:
    """Pick a namespace from --namespace, or from --task-id/$SGE_TASK_ID."""
    if args.namespace:
        return str(args.namespace).strip()

    task_id = args.task_id
    if task_id is None:
        raw = os.environ.get("SGE_TASK_ID", "").strip()
        if raw and raw.isdigit():
            task_id = int(raw)
    if task_id is None:
        return None

    ordered = [s.strip() for s in str(args.namespaces or "").split(",") if s.strip()] or available
    idx = task_id - 1
    if idx < 0 or idx >= len(ordered):
        raise PMCFetchError(
            f"--task-id {task_id} is out of range for {len(ordered)} namespaces: {ordered}"
        )
    return ordered[idx]


def _build_limiter(rps: float, jitter: float) -> MinIntervalLimiter | NullLimiter:
    """``--rps 0`` switches pacing off entirely; jitter only widens a positive rate."""
    if rps <= 0:
        return NullLimiter()
    return MinIntervalLimiter(min_interval=1.0 / rps, jitter=max(0.0, jitter))


def _cmd_namespaces(args: argparse.Namespace) -> int:
    loaded = load_records(args.corpus)
    groups = loaded.namespaces
    if args.json:
        print(json.dumps({k: len(v) for k, v in groups.items()}, indent=2))
        return 0
    width = max((len(k) for k in groups), default=9)
    print(f"{'namespace'.ljust(width)}  count")
    for name, ids in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        print(f"{name.ljust(width)}  {len(ids)}")
    print(f"{'TOTAL'.ljust(width)}  {len(loaded.records)}")
    if loaded.skipped:
        print(f"\nskipped {len(loaded.skipped)} unparseable key(s), e.g. {loaded.skipped[:3]}")
    return 0


def _cmd_download(args: argparse.Namespace) -> int:
    loaded = load_records(args.corpus)
    if loaded.skipped and not args.quiet:
        print(
            f"[pmcfetch] skipped {len(loaded.skipped)} unparseable key(s)",
            file=sys.stderr,
        )

    namespace = _resolve_namespace(args, sorted(loaded.namespaces))
    records = loaded.for_namespace(namespace)
    if args.limit and args.limit > 0:
        records = records[: args.limit]

    label = namespace or "ALL"
    if not args.quiet:
        print(f"[pmcfetch] corpus={args.corpus} namespace={label} n={len(records)}")

    if args.dry_run:
        for rec in records[:20]:
            print(f"  would fetch {rec.pmcid}  -> {rec.namespace}/")
        if len(records) > 20:
            print(f"  ... and {len(records) - 20} more")
        return 0

    if not records:
        print("[pmcfetch] nothing to do", file=sys.stderr)
        return 0

    limiter = _build_limiter(args.rps, args.jitter)
    failure_log = FailureLog(args.failed_log) if args.failed_log else None

    def _progress(res: DownloadResult, i: int, total: int) -> None:
        if args.quiet:
            return
        mark = {
            Outcome.DOWNLOADED: "ok  ",
            Outcome.SKIPPED: "skip",
            Outcome.FAILED: "FAIL",
        }[res.outcome]
        suffix = f"  {res.reason}" if res.outcome is Outcome.FAILED else ""
        print(f"[{i}/{total}] {mark} {res.pmcid}{suffix}", flush=True)

    from pmcfetch.s3source import S3OpenAccessClient

    with S3OpenAccessClient(
        timeout=args.timeout,
        retries=args.retries,
        limiter=limiter,
    ) as client:
        report = download_many(
            records,
            Path(args.out),
            client=client,
            workers=args.workers,
            by_namespace=not args.flat,
            overwrite=args.overwrite,
            verify_existing=not args.no_verify_existing,
            min_pdf_bytes=args.min_bytes,
            failure_log=failure_log,
            progress=_progress,
        )

    if args.json:
        print(json.dumps({"namespace": label, **report.as_dict()}, indent=2))
    else:
        print(
            f"\n[pmcfetch] {label}: downloaded={report.downloaded} "
            f"skipped={report.skipped} failed={report.failed} total={report.total}"
        )
        if failure_log and report.failed:
            print(f"[pmcfetch] failures logged to {failure_log.path}")

    return 1 if report.failed else 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "download":
            return _cmd_download(args)
        if args.command == "namespaces":
            return _cmd_namespaces(args)
    except PMCFetchError as exc:
        print(f"pmcfetch: {exc}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        print(f"pmcfetch: corpus file is not valid JSON: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:  # unreadable corpus, unwritable --out or --failed-log
        print(f"pmcfetch: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\npmcfetch: interrupted", file=sys.stderr)
        return 130
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
