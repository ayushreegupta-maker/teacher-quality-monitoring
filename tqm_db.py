"""
TQM admin CLI for the SQLite metadata DB. Use this to initialise the DB,
register classrooms, define activities, and assign per-day activity contexts.

The pipeline scripts (score_long_video.py, batch_score.py) will read activity
context from this DB once they're wired up — for now, this is the source of
truth you maintain manually.

Examples:
    # Initialise the DB once (idempotent)
    .venv/bin/python tqm_db.py init

    # Register a classroom (identified by camera id)
    .venv/bin/python tqm_db.py classroom add --camera D06 --name "Toddler Room A"

    # Define an activity with default context
    .venv/bin/python tqm_db.py activity add \\
        --name "Floor painting" \\
        --context "Children paint on a large paper laid on the floor with brushes and rollers." \\
        --rubrics playground,toy_design

    # Tell the system what activity ran at camera D06 on 2026-05-20
    .venv/bin/python tqm_db.py schedule set \\
        --camera D06 --date 2026-05-20 --activity "Floor painting"

    # Inspect
    .venv/bin/python tqm_db.py classroom list
    .venv/bin/python tqm_db.py activity list
    .venv/bin/python tqm_db.py schedule list --date 2026-05-20
    .venv/bin/python tqm_db.py session list --status queued
    .venv/bin/python tqm_db.py lookup --camera D06 --date 2026-05-20
"""

import argparse
import json
import logging
from datetime import date
from pathlib import Path

import adapters.db as db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)


def cmd_init(args):
    db.init_db(args.db)
    print(f"DB ready at: {args.db}")


def cmd_classroom_add(args):
    cid = db.upsert_classroom(
        school_id=args.school, camera_id=args.camera, name=args.name,
        age_range=args.age_range, default_subject=args.subject,
        db_path=args.db,
    )
    print(f"Classroom id={cid}  camera={args.camera}  name={args.name}")


def cmd_classroom_list(args):
    rows = db.list_classrooms(school_id=args.school, db_path=args.db)
    if not rows:
        print("(no classrooms yet)")
        return
    print(f"{'id':<4} {'camera':<10} {'name':<30} {'age_range':<14} {'default_subject'}")
    for r in rows:
        print(
            f"{r['id']:<4} {r['camera_id']:<10} {r['name']:<30} "
            f"{r['age_range']:<14} {r.get('default_subject') or '-'}"
        )


def cmd_activity_add(args):
    rubric_set = args.rubrics  # comma-separated string, stored as-is
    aid = db.upsert_activity(
        name=args.name, default_activity_context=args.context,
        default_rubric_set=rubric_set, db_path=args.db,
    )
    print(f"Activity id={aid}  name={args.name}")
    if args.context:
        print(f"  context: {args.context[:80]}{'…' if len(args.context) > 80 else ''}")
    if rubric_set:
        print(f"  rubrics: {rubric_set}")


def cmd_activity_list(args):
    rows = db.list_activities(db_path=args.db)
    if not rows:
        print("(no activities yet)")
        return
    for r in rows:
        ctx = (r.get("default_activity_context") or "")[:60]
        print(f"  [{r['id']}] {r['name']}")
        if ctx:
            print(f"      context: {ctx}{'…' if len(r.get('default_activity_context') or '') > 60 else ''}")
        if r.get("default_rubric_set"):
            print(f"      rubrics: {r['default_rubric_set']}")


def cmd_schedule_set(args):
    d = date.fromisoformat(args.date)
    db.set_camera_day_activity(
        camera_id=args.camera, recorded_date=d, activity_name=args.activity,
        custom_context=args.context, notes=args.notes, db_path=args.db,
    )
    print(f"Scheduled: camera={args.camera}  date={d}  activity={args.activity}")
    if args.context:
        print(f"  custom context: {args.context[:80]}{'…' if len(args.context) > 80 else ''}")


def cmd_schedule_list(args):
    d = date.fromisoformat(args.date) if args.date else None
    rows = db.list_camera_day_activities(recorded_date=d, db_path=args.db)
    if not rows:
        print("(no schedule entries)")
        return
    for r in rows:
        ctx = (r.get("activity_context") or "")[:70]
        print(
            f"  {r['recorded_date']}  camera={r['camera_id']:<10}  "
            f"activity={r['activity_name']}"
        )
        if ctx:
            print(f"      context: {ctx}{'…' if len(r.get('activity_context') or '') > 70 else ''}")
        if r.get("notes"):
            print(f"      notes: {r['notes']}")


def cmd_session_list(args):
    start = date.fromisoformat(args.start) if args.start else None
    end = date.fromisoformat(args.end) if args.end else None
    rows = db.list_sessions(
        school_id=args.school, classroom_id=args.classroom_id,
        status=args.status, start_date=start, end_date=end, db_path=args.db,
    )
    if not rows:
        print("(no sessions)")
        return
    print(f"{'session_id':<40} {'status':<10} {'camera':<10} {'recorded_at':<20} activity")
    for r in rows:
        print(
            f"{r['session_id'][:38]:<40} {r['status']:<10} "
            f"{(r.get('camera_id') or '-'):<10} {r['recorded_at'][:19]:<20} "
            f"{r.get('activity_name') or '-'}"
        )


def cmd_session_queue(args):
    """Register a video as a queued session for the batch processor to pick up.

    Resolves camera_id + recorded_at from the filename (or CLI flags), looks up
    the activity by name from the DB, and inserts a session row with
    status='queued'. Re-queueing the same video overwrites the row (session_id
    is derived from the video stem, so it's stable per file).
    """
    from pathlib import Path as _P
    from pipeline.extract import probe_duration_seconds
    from pipeline.session_resolve import resolve_session_context

    if not args.video.exists():
        print(f"ERROR: video not found: {args.video}")
        return

    explicit_date = date.fromisoformat(args.recorded_at) if args.recorded_at else None
    ctx = resolve_session_context(
        video_path=args.video.resolve(),
        camera_id=args.camera,
        recorded_at=explicit_date,
        fallback_activity_context=args.activity_context,
        activity_name_hint=args.activity,
        db_path=args.db,
    )
    if args.activity_context:
        ctx["activity_context"] = args.activity_context

    if ctx.get("activity_id") is None:
        print(
            f"ERROR: activity '{args.activity}' not found in DB. "
            f"Run: tqm_db.py activity add --name '{args.activity}' --context '...' first."
        )
        return

    # Stable session_id derived from the video file (re-queueing overwrites)
    video_slug = "".join(c if c.isalnum() else "_" for c in args.video.stem).strip("_").lower()
    session_id = f"long_full_{video_slug}"

    recorded_at_dt = ctx.get("recorded_at") or datetime.combine(
        ctx.get("recorded_date") or explicit_date or date.today(),
        datetime.min.time(),
    )

    try:
        duration_seconds = probe_duration_seconds(args.video)
    except Exception as e:
        log = logging.getLogger(__name__)
        log.warning(f"Could not probe video duration: {e!r}")
        duration_seconds = None

    db.register_session(
        session_id=session_id,
        recorded_at=recorded_at_dt,
        video_path=args.video.resolve(),
        classroom_id=ctx.get("classroom_id"),
        camera_id=ctx.get("camera_id"),
        activity_id=ctx.get("activity_id"),
        activity_context=ctx.get("activity_context"),
        status="queued",
        duration_seconds=duration_seconds,
        db_path=args.db,
    )

    print(f"Queued session: {session_id}")
    print(f"  video:       {args.video}")
    print(f"  activity:    {ctx.get('activity_name')}  (id={ctx.get('activity_id')})")
    print(f"  camera:      {ctx.get('camera_id') or '(none)'}")
    print(f"  recorded_at: {recorded_at_dt.isoformat()}")
    if duration_seconds is not None:
        print(f"  duration:    {duration_seconds:.0f}s ({duration_seconds/60:.1f} min)")
    ctx_str = ctx.get("activity_context") or "(none)"
    print(f"  context:     {ctx_str[:100]}{'…' if len(ctx_str) > 100 else ''}")


def cmd_lookup(args):
    d = date.fromisoformat(args.date)
    row = db.get_activity_for_camera_day(args.camera, d, db_path=args.db)
    if row is None:
        print(f"No activity assigned for camera={args.camera} on date={d}.")
        return
    print(json.dumps(row, indent=2))


def parse_args():
    p = argparse.ArgumentParser(description="TQM admin CLI")
    p.add_argument(
        "--db", type=Path, default=db.DEFAULT_DB_PATH,
        help=f"path to SQLite DB (default: {db.DEFAULT_DB_PATH})",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("init", help="create the DB and schema")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("classroom", help="manage classrooms")
    sp_sub = sp.add_subparsers(dest="action", required=True)
    add = sp_sub.add_parser("add", help="create or update a classroom")
    add.add_argument("--school", default="default")
    add.add_argument("--camera", required=True, help="camera id, e.g. D06")
    add.add_argument("--name", required=True, help="human-readable classroom name")
    add.add_argument("--age-range", default="3-5 years")
    add.add_argument("--subject", default=None)
    add.set_defaults(func=cmd_classroom_add)
    lst = sp_sub.add_parser("list", help="list all classrooms")
    lst.add_argument("--school", default="default")
    lst.set_defaults(func=cmd_classroom_list)

    sp = sub.add_parser("activity", help="manage activities")
    sp_sub = sp.add_subparsers(dest="action", required=True)
    add = sp_sub.add_parser("add", help="create or update an activity")
    add.add_argument("--name", required=True)
    add.add_argument("--context", default=None, help="default activity context (text)")
    add.add_argument(
        "--rubrics", default=None,
        help="comma-separated rubric labels, e.g. 'playground,toy_design'",
    )
    add.set_defaults(func=cmd_activity_add)
    lst = sp_sub.add_parser("list", help="list all activities")
    lst.set_defaults(func=cmd_activity_list)

    sp = sub.add_parser("schedule", help="manage per-day activity assignments")
    sp_sub = sp.add_subparsers(dest="action", required=True)
    setc = sp_sub.add_parser("set", help="assign activity for a camera+date")
    setc.add_argument("--camera", required=True)
    setc.add_argument("--date", required=True, help="YYYY-MM-DD")
    setc.add_argument("--activity", required=True, help="activity name (created if missing)")
    setc.add_argument("--context", default=None, help="per-day custom context (overrides activity default)")
    setc.add_argument("--notes", default=None)
    setc.set_defaults(func=cmd_schedule_set)
    lst = sp_sub.add_parser("list", help="show scheduled activities (optionally for one date)")
    lst.add_argument("--date", default=None, help="YYYY-MM-DD (optional)")
    lst.set_defaults(func=cmd_schedule_list)

    sp = sub.add_parser("session", help="inspect or queue sessions")
    sp_sub = sp.add_subparsers(dest="action", required=True)
    lst = sp_sub.add_parser("list", help="list sessions")
    lst.add_argument("--school", default="default")
    lst.add_argument("--classroom-id", type=int, default=None)
    lst.add_argument("--status", default=None, help="queued, processing, vision_done, scored, failed")
    lst.add_argument("--start", default=None, help="YYYY-MM-DD")
    lst.add_argument("--end", default=None, help="YYYY-MM-DD")
    lst.set_defaults(func=cmd_session_list)

    q = sp_sub.add_parser(
        "queue",
        help="register a video as a queued session for batch processing",
    )
    q.add_argument("--video", type=Path, required=True,
                   help="path to the video file (absolute or relative to repo root)")
    q.add_argument("--activity", required=True,
                   help="activity name — must exist in DB (use 'activity add' first)")
    q.add_argument("--camera", default=None,
                   help="camera id (e.g. D06). If omitted, parsed from the filename.")
    q.add_argument("--recorded-at", default=None,
                   help="YYYY-MM-DD; if omitted, parsed from filename, else today")
    q.add_argument("--activity-context", default=None,
                   help="override the activity's default context (advanced)")
    q.set_defaults(func=cmd_session_queue)

    sp = sub.add_parser("lookup", help="get activity context for camera+date (what the pipeline will use)")
    sp.add_argument("--camera", required=True)
    sp.add_argument("--date", required=True, help="YYYY-MM-DD")
    sp.set_defaults(func=cmd_lookup)

    return p.parse_args()


def main():
    args = parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
