"""``pmcfetch`` command line interface."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from pathlib import Path
from typing import TextIO

from pmcfetch import __version__
from pmcfetch.batch import BatchReport, DownloadResult, FailureLog, Outcome, download_many, target_path
from pmcfetch.pdf import DEFAULT_MIN_PDF_BYTES, DEFAULT_TIMEOUT
from pmcfetch.errors import PMCFetchError
from pmcfetch.manifest import DEFAULT_NAMESPACE, CorpusRecord, load_records, save_records
from pmcfetch.ratelimit import MinIntervalLimiter, NullLimiter
from pmcfetch.search import MAX_IDS_PER_QUERY, SORTS, PMCSearch

ALL = "all"


def _add_fetch_options(p: argparse.ArgumentParser) -> None:
    """Options shared by every command that downloads PDFs."""
    p.add_argument("--workers", type=int, default=4, help="Concurrent downloads (default 4)")
    p.add_argument(
        "--rps",
        type=float,
        default=0.0,
        help=(
            "Max outbound requests per second across all workers. The PMC S3 bucket "
            "is not rate limited, so the default 0 disables pacing entirely"
        ),
    )
    p.add_argument(
        "--jitter",
        type=float,
        default=0.2,
        help="Extra random spacing per request, seconds (only applies with --rps > 0)",
    )
    p.add_argument("--retries", type=int, default=4, help="Retries per article (default 4)")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="Per-request timeout")
    p.add_argument("--min-bytes", type=int, default=DEFAULT_MIN_PDF_BYTES, help="Minimum PDF size")
    p.add_argument("--overwrite", action="store_true", help="Re-download files already on disk")
    p.add_argument("--flat", action="store_true", help="Write all PDFs into --out, no subfolders")
    p.add_argument(
        "--no-verify-existing",
        action="store_true",
        help="Resume on file size alone, skipping the %%PDF magic re-check",
    )
    p.add_argument("--failed-log", default=None, help="Append failures here")
    p.add_argument("--json", action="store_true", help="Print the summary as JSON (progress goes to stderr)")
    p.add_argument("--quiet", action="store_true", help="Suppress per-article progress")


def _parse_amount(text: str) -> int | str:
    """``"all"`` or a non-negative count (``"1,000"`` is fine); raises ``ValueError``."""
    s = str(text).strip().lower().replace(",", "").replace("_", "")
    if s == ALL:
        return ALL
    n = int(s)
    if n < 0:
        raise ValueError(f"negative count: {text!r}")
    return n


def _amount_arg(text: str) -> int | str:
    try:
        return _parse_amount(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a number or 'all', got {text!r}") from None


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pmcfetch",
        description="Search the PMC Open Access Subset and download its article PDFs from AWS.",
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
    _add_fetch_options(dl)
    dl.add_argument("--limit", type=int, default=0, help="Stop after N articles (0 = all)")
    dl.add_argument("--dry-run", action="store_true", help="List what would be fetched, then exit")

    se = sub.add_parser(
        "search",
        help="Count open-access articles matching a query, then download some or all",
    )
    se.add_argument(
        "query",
        help='PMC search query, e.g. "burn scar" or "wound healing AND 2020:2024[pdat]"',
    )
    se.add_argument(
        "--download",
        metavar="N|all",
        type=_amount_arg,
        default=None,
        help=(
            "How many to download, without asking. If omitted, an interactive "
            "terminal asks after showing the count, a script only prints it, and "
            "--save saves every match without downloading"
        ),
    )
    se.add_argument("--out", default="pdfs", help="Output root directory (default pdfs)")
    se.add_argument(
        "--namespace",
        default=DEFAULT_NAMESPACE,
        help=f"Subfolder for the PDFs, and their namespace in --save (default {DEFAULT_NAMESPACE})",
    )
    se.add_argument(
        "--sort",
        choices=sorted(SORTS),
        default="relevance",
        help="Which articles come first when downloading fewer than all (default relevance)",
    )
    se.add_argument(
        "--save",
        metavar="FILE",
        default=None,
        help=(
            "Write the chosen ids to a corpus JSON file that 'pmcfetch download' reads. "
            "Without --download, every match is saved and none downloaded"
        ),
    )
    se.add_argument(
        "--api-key",
        default=None,
        help="NCBI API key, raising NCBI's limit from 3 to 10 requests/s (default $NCBI_API_KEY)",
    )
    se.add_argument("--email", default=None, help="Contact email sent to NCBI (default $NCBI_EMAIL)")
    _add_fetch_options(se)

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


def _text_out(args: argparse.Namespace) -> TextIO:
    """Where human-readable lines go: stderr under ``--json``, so stdout carries only JSON."""
    return sys.stderr if args.json else sys.stdout


def _run_downloads(
    records: list[CorpusRecord],
    args: argparse.Namespace,
    label: str,
    extra: dict | None = None,
) -> int:
    """Download *records* with the shared fetch options and print the summary."""
    if not records:
        print("[pmcfetch] nothing to do", file=sys.stderr)
        if args.json:
            print(json.dumps({**(extra or {}), "namespace": label, **BatchReport([]).as_dict()}, indent=2))
        return 0

    limiter = _build_limiter(args.rps, args.jitter)
    failure_log = FailureLog(args.failed_log) if args.failed_log else None
    out = _text_out(args)

    def _progress(res: DownloadResult, i: int, total: int) -> None:
        if args.quiet:
            return
        mark = {
            Outcome.DOWNLOADED: "ok  ",
            Outcome.SKIPPED: "skip",
            Outcome.FAILED: "FAIL",
        }[res.outcome]
        suffix = f"  {res.reason}" if res.outcome is Outcome.FAILED else ""
        print(f"[{i}/{total}] {mark} {res.pmcid}{suffix}", file=out, flush=True)

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
        print(json.dumps({**(extra or {}), "namespace": label, **report.as_dict()}, indent=2))
    else:
        print(
            f"\n[pmcfetch] {label}: downloaded={report.downloaded} "
            f"skipped={report.skipped} failed={report.failed} total={report.total}"
        )
        if failure_log and report.failed:
            print(f"[pmcfetch] failures logged to {failure_log.path}")

    return 1 if report.failed else 0


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
        print(f"[pmcfetch] corpus={args.corpus} namespace={label} n={len(records)}", file=_text_out(args))

    if args.dry_run:
        if args.json:
            plan = [{"pmcid": rec.pmcid, "namespace": rec.namespace} for rec in records]
            summary = {"namespace": label, "dry_run": True, "total": len(records), "would_fetch": plan}
            print(json.dumps(summary, indent=2))
            return 0
        for rec in records[:20]:
            print(f"  would fetch {rec.pmcid}  -> {rec.namespace}/")
        if len(records) > 20:
            print(f"  ... and {len(records) - 20} more")
        return 0

    return _run_downloads(records, args, label)


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _ask_amount(count: int) -> int | str:
    """Ask how many of *count* matches to download; Enter or end of input means none."""
    while True:
        try:
            answer = input(f"How many to download? [all / a number up to {count:,} / Enter for none]: ")
        except EOFError:
            return 0
        if not answer.strip():
            return 0
        try:
            return _parse_amount(answer)
        except ValueError:
            print("  Type 'all', a number, or press Enter to skip.")


def _cmd_search(args: argparse.Namespace) -> int:
    search = PMCSearch(
        api_key=args.api_key or os.environ.get("NCBI_API_KEY") or None,
        email=args.email or os.environ.get("NCBI_EMAIL") or None,
        timeout=args.timeout,
        retries=args.retries,
    )
    summary = search.summarize(args.query)
    info = {
        "query": summary.query,
        "matches": summary.count,
        "translation": summary.translation,
        "warnings": list(summary.warnings),
    }
    if not args.json:
        print(f"[pmcfetch] {summary.count:,} open-access article(s) match {summary.query!r}")
        if summary.translation and not args.quiet:
            print(f"[pmcfetch] NCBI searched: {summary.translation}")
    for warning in summary.warnings:
        print(f"[pmcfetch] warning: {warning}", file=sys.stderr)

    # --save without --download keeps every match and downloads none, so the
    # list can go to `pmcfetch download` later, e.g. in an HPC array job.
    save_only = args.download is None and args.save is not None
    amount = ALL if save_only else args.download
    if amount is None and summary.count and not args.json and _interactive():
        amount = _ask_amount(summary.count)
    if amount is None:
        if args.json:
            print(json.dumps(info, indent=2))
        elif summary.count:
            print(f"[pmcfetch] to download, rerun with --download N or --download all (saves to {args.out}/)")
        return 0

    limit = None if amount == ALL else min(int(amount), summary.count)
    records: list[CorpusRecord] = []
    if limit != 0 and summary.count:
        if args.sort == "relevance" and (limit or summary.count) > MAX_IDS_PER_QUERY and not args.quiet:
            print(
                f"[pmcfetch] note: NCBI ranks only the first {MAX_IDS_PER_QUERY:,} by relevance; "
                "the rest follow newest first",
                file=sys.stderr,
            )

        def _id_progress(collected: int, wanted: int) -> None:
            if not args.quiet:
                print(f"[pmcfetch] collecting ids: {collected:,}/{wanted:,}", file=sys.stderr, flush=True)

        records = search.records(
            args.query, limit=limit, sort=args.sort, namespace=args.namespace, progress=_id_progress
        )

    chatty = not (args.quiet or args.json)
    if args.save is not None:
        # Written even when empty, so a script that asked for a file always gets one.
        path = save_records(records, args.save)
        info.update(saved=len(records), saved_to=str(path))
        if chatty:
            print(f"[pmcfetch] saved {len(records):,} id(s) to {path}")
            if save_only and records:
                cmd = f"pmcfetch download --corpus {shlex.quote(str(path))} --out {shlex.quote(args.out)}"
                print(f"[pmcfetch] to download them: {cmd}")
    if save_only or not records:
        if args.json:
            print(json.dumps(info, indent=2))
        return 0
    if chatty:
        folder = target_path(args.out, records[0], by_namespace=not args.flat).parent
        print(f"[pmcfetch] downloading {len(records):,} article(s) to {folder}/")
    return _run_downloads(records, args, args.namespace, extra=info)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "download":
            return _cmd_download(args)
        if args.command == "search":
            return _cmd_search(args)
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
