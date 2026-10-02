"""Command line entry point. Non-interactive; safe for unattended cron."""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from pathlib import Path

from . import __version__, compare as compare_mod, snapshot
from .corpus import DEFAULT_ROOT


def _latest_previous(out_dir: Path, exclude: Path | None) -> Path | None:
    snaps = sorted(out_dir.glob("snapshot-*.json"))
    snaps = [p for p in snaps if exclude is None or p.resolve() != exclude.resolve()]
    return snaps[-1] if snaps else None


def _load(path: Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _try_load(path: Path) -> dict | None:
    """Load a snapshot, or None (with a warning) if it is unreadable or corrupt."""
    try:
        data = _load(path)
    except (OSError, ValueError) as exc:
        print(f"warning: cannot read snapshot {path}: {exc}", file=sys.stderr)
        return None
    if not isinstance(data, dict):
        print(f"warning: snapshot {path} is not a JSON object", file=sys.stderr)
        return None
    return data


def _write_atomic(path: Path, text: str) -> None:
    """Write via a temp file and rename, so an interrupted cron run never
    leaves a truncated snapshot for the next run to pick up as 'previous'."""
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="claude-friction-audit",
        description="Repeatable, measurable friction audit over Claude Code transcripts.",
    )
    ap.add_argument("--root", default=str(DEFAULT_ROOT), help="transcripts root")
    ap.add_argument("--out-dir", default="out", help="where snapshots/reports land")
    ap.add_argument("--compare", nargs=2, metavar=("OLD", "NEW"), help="diff two snapshots and exit")
    ap.add_argument("--previous", help="explicit previous snapshot for deltas")
    ap.add_argument("--no-report", action="store_true", help="snapshot only")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--version", action="version", version=__version__)
    args = ap.parse_args(argv)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # --- compare mode -----------------------------------------------------
    if args.compare:
        old_p, new_p = Path(args.compare[0]), Path(args.compare[1])
        for p in (old_p, new_p):
            if not p.is_file():
                print(f"error: no such snapshot: {p}", file=sys.stderr)
                return 2
        old, new = _try_load(old_p), _try_load(new_p)
        if old is None or new is None:
            print("error: cannot compare a corrupt snapshot", file=sys.stderr)
            return 2
        diff = compare_mod.compare(old, new)
        print(json.dumps(diff, indent=2))
        _print_diff_table(diff)
        return 0

    # --- audit mode -------------------------------------------------------
    try:
        snap = snapshot.build(args.root, progress=not args.quiet)
    except Exception:
        traceback.print_exc()
        print("error: sweep failed", file=sys.stderr)
        return 1

    if snap["corpus"]["files"] == 0:
        print(f"error: no transcripts found under {args.root}", file=sys.stderr)
        return 1

    snap_path = out_dir / snapshot.default_filename(snap)
    _write_atomic(snap_path, json.dumps(snap, indent=1))
    if not args.quiet:
        print(f"snapshot -> {snap_path}", file=sys.stderr)

    prev_path = Path(args.previous) if args.previous else _latest_previous(out_dir, snap_path)
    prev = _try_load(prev_path) if prev_path and prev_path.is_file() else None
    diff = compare_mod.compare(prev, snap) if prev else None

    if not args.no_report:
        # Imported lazily so a reporting bug can never cost you the snapshot,
        # which is the expensive artefact.
        from . import report

        html = report.render(snap, prev, diff, prev_path=str(prev_path) if prev else None)
        rep_path = out_dir / f"report-{snapshot.default_filename(snap)[9:-5]}.html"
        _write_atomic(rep_path, html)
        if not args.quiet:
            print(f"report   -> {rep_path}", file=sys.stderr)

    if not args.quiet:
        _print_summary(snap, diff)
    return 0


def _print_summary(snap: dict, diff: dict | None) -> None:
    c, t = snap["corpus"], snap["token_burn"]
    print(
        f"\ncorpus: {c['files']} files / {c['main_sessions']} main + "
        f"{c['subagent_sessions']} subagent / {c['projects']} projects "
        f"({c['first_event'][:10] if c['first_event'] else '?'} -> "
        f"{c['last_event'][:10] if c['last_event'] else '?'})",
        file=sys.stderr,
    )
    print(
        f"tokens: {t['total']['total']:,} | main-thread {t['main_thread_share']:.1%} "
        f"| cache-read {t['cache_read_share']:.1%}",
        file=sys.stderr,
    )
    if c["bad_lines"] or c["failed_files"]:
        print(
            f"note: {c['bad_lines']} unparseable lines, {c['failed_files']} failed files (skipped)",
            file=sys.stderr,
        )
    if diff:
        _print_diff_table(diff)


def _print_diff_table(diff: dict) -> None:
    print("\nvs previous run:", file=sys.stderr)
    sym = {"up": "^", "down": "v", "flat": "=", "new": "*"}
    for r in diff["rows"]:
        if r["delta"] is None:
            continue
        pct = f" ({r['pct']:+.1%})" if r["pct"] is not None else ""
        print(
            f"  {sym.get(r['arrow'], '?')} {r['label']:<38} {r['old']} -> {r['new']}"
            f"{pct}  [{r['verdict']}]",
            file=sys.stderr,
        )
    print("\nfix verdicts:", file=sys.stderr)
    for f in diff["fix_verdicts"]:
        print(f"  [{f['verdict']}] {f['fix']}\n      {f['why']}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
