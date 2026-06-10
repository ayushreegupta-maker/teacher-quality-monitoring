"""
TQM consolidation migration — shared helpers + reverse subcommand.

Every migration step script imports these helpers so it logs file moves in a
consistent way. The log lives at PROJECT_ROOT/migration_log.txt and is the
authoritative trail of every `mv` the migration does — independent of git,
because most of the data being moved is gitignored.

Subcommands
-----------
reverse        Undo file moves recorded in migration_log.txt.

  --step N        Reverse all moves tagged with step=N
  --since TS      Reverse all moves logged at or after ISO timestamp TS
  --last          Reverse only the most recent move
  --dry-run       Print what would happen; touch nothing
  --yes           Skip the interactive y/N confirmation

Examples
--------
  python scripts/migrate.py reverse --step 1 --dry-run
  python scripts/migrate.py reverse --since "2026-06-10 12:00"
  python scripts/migrate.py reverse --last --yes

Module API (used by step scripts)
---------------------------------
  PROJECT_ROOT, LOG_PATH
  append_log_entry(step, old_path, new_path, note="")
  atomic_mv_with_log(old_path, new_path, step, note="") -> bool
  require_confirmation(plan_lines, auto_yes=False) -> bool
  parse_log() -> list[LogEntry]

The mv is atomic only when source + destination live on the same filesystem
(POSIX rename semantics). All migration moves stay inside the project root,
so this holds.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable


PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = PROJECT_ROOT / "migration_log.txt"

# Tab-separated columns in migration_log.txt:
#   1. ISO timestamp (UTC, microsecond precision)
#   2. step label, e.g. "1" / "2" / "reverse-of-1"
#   3. old path (absolute)
#   4. new path (absolute)
#   5. optional note (no tabs)
LOG_COLUMNS = ("timestamp", "step", "old_path", "new_path", "note")


@dataclass(frozen=True)
class LogEntry:
    timestamp: str
    step: str
    old_path: Path
    new_path: Path
    note: str

    @classmethod
    def from_line(cls, line: str) -> "LogEntry | None":
        parts = line.rstrip("\n").split("\t")
        if len(parts) < 4:
            return None
        ts, step, old, new = parts[:4]
        note = parts[4] if len(parts) >= 5 else ""
        return cls(
            timestamp=ts,
            step=step,
            old_path=Path(old),
            new_path=Path(new),
            note=note,
        )


def parse_log() -> list[LogEntry]:
    """Return all entries currently in migration_log.txt, in chronological
    order. Empty list if the log doesn't exist."""
    if not LOG_PATH.exists():
        return []
    entries: list[LogEntry] = []
    for line in LOG_PATH.read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        e = LogEntry.from_line(line)
        if e is not None:
            entries.append(e)
    return entries


def _now_iso() -> str:
    """ISO timestamp with microsecond precision, UTC, no timezone suffix.
    Stable, sortable string."""
    return datetime.utcnow().isoformat(timespec="microseconds")


def append_log_entry(
    step: str | int, old_path: Path, new_path: Path, note: str = ""
) -> None:
    """Append a single entry to migration_log.txt. The log is the source of
    truth for what's been moved; never edit it by hand."""
    if "\t" in note or "\n" in note:
        raise ValueError("note must not contain tabs or newlines")
    line = "\t".join(
        [
            _now_iso(),
            str(step),
            str(old_path.resolve()),
            str(new_path.resolve()),
            note,
        ]
    )
    # Write a header on first creation so the file is self-describing.
    create_header = not LOG_PATH.exists()
    with LOG_PATH.open("a") as f:
        if create_header:
            f.write(
                "# TQM migration log — append-only audit trail of every `mv`.\n"
                "# Format: <ISO-UTC-timestamp>\\t<step>\\t<old_path>\\t<new_path>\\t<note?>\n"
            )
        f.write(line + "\n")


def atomic_mv_with_log(
    old_path: Path,
    new_path: Path,
    step: str | int,
    note: str = "",
    refuse_overwrite: bool = True,
) -> bool:
    """Move a file or directory from `old_path` to `new_path` atomically (when
    same-filesystem) and append a log entry.

    Returns True on success, False on skip. Raises on hard failure.

    Safety:
      - Source must exist; otherwise False (skip).
      - Destination's parent dir is created (mkdir -p).
      - If destination exists and `refuse_overwrite` (default), False (skip)
        — never silently clobbers.
      - Source and destination must be on the same filesystem; otherwise the
        atomic guarantee is gone and we refuse to proceed.
    """
    old_path = old_path.resolve()
    new_path = new_path.resolve()

    if not old_path.exists():
        print(f"  SKIP (missing source): {old_path}")
        return False

    if new_path.exists() and refuse_overwrite:
        print(f"  SKIP (destination exists): {new_path}")
        return False

    # Same-filesystem check. os.stat().st_dev tells us the device id.
    src_dev = old_path.stat().st_dev
    dst_parent = new_path.parent
    # Walk up to find an existing ancestor so we can stat it.
    probe = dst_parent
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    dst_dev = probe.stat().st_dev
    if src_dev != dst_dev:
        raise RuntimeError(
            f"refusing cross-filesystem move ({old_path} → {new_path}); "
            "atomic mv requires same filesystem"
        )

    dst_parent.mkdir(parents=True, exist_ok=True)
    # os.rename is atomic on POSIX same-filesystem. shutil.move would fall back
    # to copy+delete across filesystems; we already guarded against that above.
    os.rename(old_path, new_path)
    append_log_entry(step=step, old_path=old_path, new_path=new_path, note=note)
    print(f"  mv  {_short(old_path)}  →  {_short(new_path)}")
    return True


def _short(p: Path) -> str:
    """Render a path relative to PROJECT_ROOT for shorter log printing."""
    try:
        return str(p.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(p)


def require_confirmation(plan_lines: Iterable[str], auto_yes: bool = False) -> bool:
    """Print a plan, prompt y/N (unless auto_yes). Returns whether to proceed."""
    print()
    print("─" * 78)
    print("PLAN — review carefully before confirming:")
    print("─" * 78)
    for line in plan_lines:
        print(line)
    print("─" * 78)
    if auto_yes:
        print("--yes given → proceeding without prompt.")
        return True
    answer = input("Proceed? [y/N] ").strip().lower()
    return answer in ("y", "yes")


def _filter_for_reverse(args: argparse.Namespace) -> list[LogEntry]:
    """Pick which log entries to reverse based on --step / --since / --last."""
    entries = parse_log()
    if not entries:
        return []
    if args.last:
        return [entries[-1]]
    if args.step is not None:
        target = str(args.step)
        return [e for e in entries if e.step == target]
    if args.since is not None:
        return [e for e in entries if e.timestamp >= args.since]
    raise SystemExit("reverse: must pass one of --step, --since, --last")


def cmd_reverse(args: argparse.Namespace) -> None:
    targets = _filter_for_reverse(args)
    if not targets:
        print("No matching log entries — nothing to reverse.")
        return

    # Walk backwards so nested moves unwind in the right order.
    targets = list(reversed(targets))

    plan_lines = [
        f"REVERSE {len(targets)} move(s):",
        "",
    ]
    for e in targets:
        plan_lines.append(
            f"  step={e.step}  {_short(e.new_path)}  →  {_short(e.old_path)}"
        )
    plan_lines.append("")
    plan_lines.append(
        "Each move is mv(new_path → old_path). New entries will be appended"
    )
    plan_lines.append("to migration_log.txt with step='reverse-of-<original>'.")

    if args.dry_run:
        for line in plan_lines:
            print(line)
        print("\n--dry-run given → nothing actually moved.")
        return

    if not require_confirmation(plan_lines, auto_yes=args.yes):
        print("Aborted by user.")
        return

    reversed_count = 0
    for e in targets:
        ok = atomic_mv_with_log(
            old_path=e.new_path,
            new_path=e.old_path,
            step=f"reverse-of-{e.step}",
            note=f"reversing entry from {e.timestamp}",
        )
        if ok:
            reversed_count += 1
    print(f"\nReversed {reversed_count} of {len(targets)} entries.")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="migrate",
        description="TQM consolidation migration helpers.",
    )
    sub = p.add_subparsers(dest="subcommand", required=True)

    rev = sub.add_parser(
        "reverse",
        help="Undo file moves recorded in migration_log.txt.",
        description=(
            "Undo file moves from migration_log.txt, selecting which moves "
            "to reverse via --step, --since, or --last."
        ),
    )
    sel = rev.add_mutually_exclusive_group(required=True)
    sel.add_argument(
        "--step", help="Reverse all moves logged with this step label, e.g. 1"
    )
    sel.add_argument(
        "--since",
        help='Reverse all moves at or after this ISO timestamp, e.g. "2026-06-10T12:00:00"',
    )
    sel.add_argument(
        "--last",
        action="store_true",
        help="Reverse only the most recent logged move",
    )
    rev.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the plan and exit without moving anything",
    )
    rev.add_argument(
        "--yes", action="store_true", help="Skip the interactive y/N prompt"
    )
    rev.set_defaults(func=cmd_reverse)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
