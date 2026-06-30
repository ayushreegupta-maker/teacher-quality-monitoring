"""
Streamlit dashboard for the Openhouse quality team.

Run:
    .venv/bin/streamlit run frontend/quality_app.py

Reads the existing pipeline artefacts directly — no DB:
  data/tqm_answers.xlsx     ← rolling Q&A log (one row per Q × run)
  data/rubric_runs/<subj>/<config_slug>/5_answers.json     (full payload)
  data/rubric_runs/<subj>/<config_slug>/0_config.json      (activity_context)
  data/sessions/<subj>/<sid>/3_trimmed.mp4                  (player source)
  data/cctv_cameras.xlsx                                    (camera → centre)

Two-pane layout:
  LEFT  — sessions list (one row per session — the "canonical" run only).
  RIGHT — selected session: video at top, materials + class window beside it,
          then four tabs (Environment / Content Knowledge / Facilitation /
          Warmth). Click an evidence timestamp anywhere → video seeks.
"""
from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
ANSWERS_XLSX = ROOT / "data" / "tqm_answers.xlsx"
RUBRIC_RUNS = ROOT / "data" / "rubric_runs"
SESSIONS_DIR = ROOT / "data" / "sessions"
CAMERAS_XLSX = ROOT / "data" / "cctv_cameras.xlsx"

SUBJECT_SHEETS = {
    "art": "Art",
    "public_speaking": "Public Speaking",
    "robotics": "Robotics",
}

# 5-min margin on each side of the class window (the convention I use when
# manually trimming via the fast path). Used only to estimate class end time.
TRIM_MARGIN_MIN = 5

SECTION_TABS = ("Environment", "Content Knowledge", "Facilitation", "Warmth")
SECTION_ICONS = {
    "Environment": "🏛️",
    "Content Knowledge": "📚",
    "Facilitation": "🤝",
    "Warmth": "💛",
}

st.set_page_config(
    page_title="Openhouse",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ─── Cloud detection + shared-password gate ──────────────────────────────────


def is_cloud_deploy() -> bool:
    """True when running on Streamlit Community Cloud (or any deploy where
    `is_cloud_deploy = true` is set in st.secrets). False when running
    locally without secrets."""
    try:
        return bool(st.secrets.get("is_cloud_deploy", False))
    except Exception:
        return False


def password_gate() -> bool:
    """Shared-password lock used in cloud deploys (free tier has no built-in
    viewer auth). Returns True when the user is authenticated for this
    session; False otherwise (and renders the login UI as a side effect).

    Local runs without a `password` secret skip the gate entirely.
    """
    try:
        expected = st.secrets.get("password", None)
    except Exception:
        expected = None
    if expected is None:
        return True  # no secret configured → no gate (typical local dev)
    if st.session_state.get("_auth_ok"):
        return True

    with st.container():
        st.title("Openhouse")
        st.caption("Sign in to view the dashboard")
        entered = st.text_input("Password", type="password")
        if entered:
            if entered == expected:
                st.session_state["_auth_ok"] = True
                st.rerun()
            else:
                st.error("Incorrect password.")
    return False


# ─── Data loaders ──────────────────────────────────────────────────────────


@st.cache_data(ttl=60)
def load_runs_sheet() -> pd.DataFrame:
    """The Runs tab — one row per rubric run, with materials_seen_json column."""
    try:
        return pd.read_excel(ANSWERS_XLSX, sheet_name="Runs")
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=60)
def materials_by_run() -> dict[str, list]:
    """{run_id → parsed materials_seen list}. Reads the Runs sheet's
    materials_seen_json column so the cloud build doesn't need per-run JSON.
    """
    runs = load_runs_sheet()
    out: dict[str, list] = {}
    if runs.empty or "materials_seen_json" not in runs.columns:
        return out
    for _, row in runs.iterrows():
        rid = str(row.get("run_id") or "").strip()
        raw = row.get("materials_seen_json")
        if not rid or pd.isna(raw) or not str(raw).strip():
            continue
        try:
            out[rid] = json.loads(str(raw))
        except Exception:
            continue
    return out


@st.cache_data(ttl=60)
def load_all_answers() -> pd.DataFrame:
    """Concat the three subject sheets into one tall dataframe."""
    frames = []
    for subj, sheet in SUBJECT_SHEETS.items():
        try:
            df = pd.read_excel(ANSWERS_XLSX, sheet_name=sheet)
        except Exception:
            continue
        df["subject"] = subj
        frames.append(df)
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    if "insufficient_information" in df.columns:
        df["insufficient_information"] = (
            df["insufficient_information"].astype(str).str.lower().eq("true")
        )
    if "session_date" in df.columns:
        df["session_date"] = pd.to_datetime(df["session_date"], errors="coerce").dt.date
    return df


@st.cache_data(ttl=300)
def load_camera_lookup() -> dict[str, dict]:
    """{camera_id: {centre_name, subject}} from cctv_cameras.xlsx."""
    import openpyxl
    try:
        wb = openpyxl.load_workbook(CAMERAS_XLSX, data_only=True)
    except Exception:
        return {}
    ws = wb["cameras"] if "cameras" in wb.sheetnames else wb.active
    headers = [c.value for c in ws[1]]
    out = {}
    for row in ws.iter_rows(min_row=2, values_only=True):
        rec = dict(zip(headers, row))
        cam = rec.get("camera_id")
        if not cam:
            continue
        out[str(cam)] = {
            "centre_name": (rec.get("centre_name") or "").strip() or "—",
        }
    return out


@st.cache_data(ttl=60)
def index_run_artefacts() -> dict[str, dict]:
    """Walk data/rubric_runs/<subject>/<config_slug>/ and return
    {run_id_normalised: {subject, run_dir, has_activity_context, has_materials_seen, shape}}.

    run_id_normalised = the colons-stripped run_id (matches config_slug prefix).
    """
    index: dict[str, dict] = {}
    if not RUBRIC_RUNS.exists():
        return index
    for subj_dir in RUBRIC_RUNS.iterdir():
        if not subj_dir.is_dir():
            continue
        for run_dir in subj_dir.iterdir():
            if not run_dir.is_dir():
                continue
            cfg_path = run_dir / "0_config.json"
            ans_path = run_dir / "5_answers.json"
            if not cfg_path.exists() or not ans_path.exists():
                continue
            try:
                cfg = json.loads(cfg_path.read_text())
            except Exception:
                cfg = {}
            run_id_norm = run_dir.name.split("__", 1)[0]  # YYYY-MM-DDTHHMMSS
            has_ctx = bool(cfg.get("activity_context"))
            try:
                ans = json.loads(ans_path.read_text())
                has_materials = bool(ans.get("materials_seen"))
            except Exception:
                has_materials = False
            index[run_id_norm] = {
                "subject": subj_dir.name,
                "run_dir": run_dir,
                "has_activity_context": has_ctx,
                "has_materials_seen": has_materials,
                "shape": cfg.get("shape") or "?",
                "activity_context": cfg.get("activity_context"),
            }
    return index


def _norm_run_id(run_id: str) -> str:
    """'2026-06-29T12:54:16' → '2026-06-29T125416' (matches config_slug prefix)."""
    return str(run_id).replace(":", "")


@st.cache_data(ttl=60)
def canonical_session_runs(answers_df: pd.DataFrame) -> pd.DataFrame:
    """Pick the best (subject, session_id) → one canonical run.

    Preference order:
      1. Shape B with activity_context (the "Shape B with session context")
      2. Shape B without activity_context
      3. Skip Shape A entirely (we only surface productionable Shape B runs)

    Within the chosen tier, take the latest run_n.
    """
    if answers_df.empty:
        return answers_df
    runs_meta = index_run_artefacts()

    # Strip to one row per (subject, session_id, run_id)
    cols = ["subject", "session_id", "session_date", "camera", "teacher_id",
            "rubric_version", "reasoner", "shape", "run_id", "run_n"]
    runs = answers_df[cols].drop_duplicates().copy()
    runs = runs[runs["shape"] == "B"]
    if runs.empty:
        return runs

    runs["run_norm"] = runs["run_id"].map(_norm_run_id)
    runs["has_ctx"] = runs["run_norm"].map(
        lambda r: runs_meta.get(r, {}).get("has_activity_context", False)
    )
    runs["has_materials"] = runs["run_norm"].map(
        lambda r: runs_meta.get(r, {}).get("has_materials_seen", False)
    )

    # tier 1 = has_ctx; tier 2 = no ctx. Pick highest tier per session, then latest run_n.
    runs["tier"] = runs["has_ctx"].map({True: 1, False: 2})
    runs = runs.sort_values(
        ["subject", "session_id", "tier", "run_n"],
        ascending=[True, True, True, False],
    )
    return runs.drop_duplicates(subset=["subject", "session_id"], keep="first")


def load_run_answers_json(subject: str, run_id: str) -> dict | None:
    """Read 5_answers.json for the matching run."""
    idx = index_run_artefacts()
    info = idx.get(_norm_run_id(run_id))
    if info is None:
        return None
    p = info["run_dir"] / "5_answers.json"
    if not p.exists():
        return None
    return json.loads(p.read_text())


def trimmed_video_path(subject: str, session_id: str) -> Path | None:
    p = SESSIONS_DIR / subject / session_id / "3_trimmed.mp4"
    return p if p.exists() else None


def ffprobe_duration_seconds(path: Path) -> float | None:
    """ffprobe the file duration in seconds. Cached to avoid re-probing."""
    import subprocess
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_format", "-print_format", "json", str(path)],
            capture_output=True, text=True, timeout=15,
        )
        return float(json.loads(r.stdout)["format"]["duration"])
    except Exception:
        return None


@st.cache_data(ttl=300)
def cached_video_duration(path_str: str) -> float | None:
    return ffprobe_duration_seconds(Path(path_str))


def hms_to_seconds(ts: str) -> int:
    try:
        parts = [int(x) for x in str(ts).split(":")]
        if len(parts) == 3:
            return parts[0] * 3600 + parts[1] * 60 + parts[2]
        if len(parts) == 2:
            return parts[0] * 60 + parts[1]
    except Exception:
        pass
    return 0


def parse_class_window(
    subject: str, session_id: str, trimmed_path: Path | None
) -> tuple[str, str, str] | None:
    """Return (start_HHMM, end_HHMM, source_note) for the class window.

    Start: HHMM token from session_id (scheduled start).
    End  : depends on how the trim was produced —
      - If 2_boundaries.json exists, the trim is boundary-detected
        (no margin); end = start + full trim duration.
      - Otherwise the trim is the manual fast path which includes
        TRIM_MARGIN_MIN on each side; end = start + (trim - 2×margin).

    source_note describes which formula was applied so the UI can show
    "approximate" honestly. Returns None if session_id is malformed.
    """
    try:
        parts = session_id.split("__")
        hhmm = parts[-1]
        h, m = int(hhmm[:2]), int(hhmm[2:])
        start = time(h, m)
    except Exception:
        return None
    if trimmed_path is None or not trimmed_path.exists():
        return (start.strftime("%H:%M"), "—", "no trim available")
    dur_min = max(1, int(round((cached_video_duration(str(trimmed_path)) or 0) / 60)))
    bd_path = trimmed_path.parent / "2_boundaries.json"
    if bd_path.exists():
        class_min = dur_min
        source = "boundary-detected"
    else:
        class_min = max(1, dur_min - 2 * TRIM_MARGIN_MIN)
        source = f"manual trim (−{2*TRIM_MARGIN_MIN}min margin)"
    end_dt = datetime.combine(date.today(), start) + timedelta(minutes=class_min)
    return (start.strftime("%H:%M"), end_dt.time().strftime("%H:%M"), source)


# ─── App ───────────────────────────────────────────────────────────────────


def main():
    if not password_gate():
        return
    st.title("Openhouse")

    df = load_all_answers()
    if df.empty:
        st.warning(
            f"No data found in `{ANSWERS_XLSX.relative_to(ROOT)}`. "
            "Run `scripts/run_rubric.py` on at least one session first."
        )
        return

    canon = canonical_session_runs(df)
    cam_lookup = load_camera_lookup()

    canon = canon.copy()
    canon["centre"] = canon["camera"].map(
        lambda c: cam_lookup.get(str(c), {}).get("centre_name", "—")
    )
    canon["teacher_display"] = canon["teacher_id"].apply(
        lambda t: t if pd.notna(t) and str(t) not in ("None", "nan", "") else "—"
    )

    # ── Sidebar: filters + sessions list ──
    # All filters are OPTIONAL. Empty multiselect = "no constraint" (don't
    # apply that filter). Date range left blank = full available range.
    st.sidebar.header("Filters")

    all_subjects = sorted(canon["subject"].unique())
    sel_subjects = st.sidebar.multiselect(
        "Subject", all_subjects, default=[], placeholder="Any",
    )

    valid_dates = sorted(canon["session_date"].dropna().unique())
    if valid_dates:
        date_range = st.sidebar.date_input(
            "Date range",
            value=(),
            min_value=valid_dates[0],
            max_value=valid_dates[-1],
        )
        if isinstance(date_range, tuple) and len(date_range) == 2:
            d_lo, d_hi = date_range
        else:
            d_lo, d_hi = None, None
    else:
        d_lo, d_hi = None, None

    all_centres = sorted(canon["centre"].dropna().unique())
    sel_centres = st.sidebar.multiselect(
        "Centre", all_centres, default=[], placeholder="Any",
    )

    all_teachers = sorted(canon["teacher_display"].dropna().unique())
    sel_teachers = st.sidebar.multiselect(
        "Teacher", all_teachers, default=[], placeholder="Any",
    )

    # Apply each filter only when the user actually set it
    filtered = canon
    if sel_subjects:
        filtered = filtered[filtered["subject"].isin(sel_subjects)]
    if sel_centres:
        filtered = filtered[filtered["centre"].isin(sel_centres)]
    if sel_teachers:
        filtered = filtered[filtered["teacher_display"].isin(sel_teachers)]
    if d_lo is not None and d_hi is not None:
        filtered = filtered[
            (filtered["session_date"] >= d_lo)
            & (filtered["session_date"] <= d_hi)
        ]
    filtered = filtered.sort_values("session_date", ascending=False)

    st.sidebar.markdown("---")
    st.sidebar.metric("Sessions", len(filtered))

    if filtered.empty:
        st.sidebar.info("No sessions match the current filters.")
        st.info("Use the filters in the sidebar to load a session.")
        return

    st.sidebar.subheader("Pick a session")
    labels: list[str] = []
    keys: list[tuple[str, str, str]] = []
    for _, row in filtered.iterrows():
        sd = row["session_date"]
        sd_str = sd.strftime("%Y-%m-%d") if isinstance(sd, date) else str(sd)
        label = (
            f"**{row['subject']}** · {sd_str}\n\n"
            f"{row['centre']} · {row['teacher_display']}"
        )
        labels.append(label)
        keys.append((row["subject"], row["session_id"], row["run_id"]))

    idx = st.sidebar.radio(
        "Pick a session",
        range(len(labels)),
        format_func=lambda i: labels[i],
        label_visibility="collapsed",
    )
    sel_subject, sel_sid, sel_run_id = keys[idx]

    # ── Main pane: full-width session detail ──
    render_session_detail(df, sel_subject, sel_sid, sel_run_id, cam_lookup)


def render_session_detail(
    df: pd.DataFrame, subject: str, session_id: str, run_id: str,
    cam_lookup: dict[str, dict],
):
    artefacts = load_run_answers_json(subject, run_id) or {}
    qdf = df[(df["subject"] == subject) & (df["run_id"] == run_id)].copy()
    # materials live in the Runs sheet of the xlsx now, so the cloud build
    # works without the per-run JSON. Falls back to artefacts if absent.
    materials_idx = materials_by_run()
    materials_from_xlsx = materials_idx.get(str(run_id))

    cam = qdf["camera"].iloc[0] if not qdf.empty else ""
    centre = cam_lookup.get(str(cam), {}).get("centre_name", "—")
    teacher = qdf["teacher_id"].iloc[0] if not qdf.empty else None
    teacher_display = teacher if pd.notna(teacher) and str(teacher) not in ("None", "nan", "") else "—"
    sess_date = qdf["session_date"].iloc[0] if not qdf.empty else None
    sess_date_str = sess_date.strftime("%Y-%m-%d") if isinstance(sess_date, date) else str(sess_date)

    # Header (no session_id visible)
    st.subheader(f"{subject} · {sess_date_str}")
    st.caption(f"Centre: **{centre}**  ·  Teacher: **{teacher_display}**")

    # ── Anchor for scroll-to-video, then persistent video player ──
    st.markdown('<div id="video-anchor"></div>', unsafe_allow_html=True)
    v_path = trimmed_video_path(subject, session_id)
    video_state_key = f"video_jump_{session_id}_{run_id}"
    jump_to = int(st.session_state.get(video_state_key, 0))

    if v_path is not None:
        st.video(str(v_path), start_time=jump_to)
        # ALWAYS render the caption (placeholder when no jump) so the
        # widget tree stays positionally stable across reruns — otherwise
        # st.expander state below resets when a timestamp is clicked.
        if jump_to:
            ts_str = f"{jump_to // 3600:02d}:{(jump_to // 60) % 60:02d}:{jump_to % 60:02d}"
            st.caption(f"Player seeking to `{ts_str}`. Tap another timestamp to jump again.")
        else:
            st.caption(" ")
    elif is_cloud_deploy():
        st.info(
            "🎬 **Video playback is disabled in the hosted dashboard.** "
            "Open the local copy on the team Mac to play the trimmed class "
            "videos. The Q&A, materials, and class window below are the "
            "same in both."
        )
    else:
        st.info(
            "No trimmed video on disk for this session. "
            "Run the pipeline (or manually trim) to populate."
        )

    # ALWAYS render the scroll injector for the same reason — pass do_scroll
    # so it knows whether to actually scroll on this render.
    components_html_scroll_to_anchor(
        "video-anchor",
        do_scroll=bool(st.session_state.pop("_scroll_to_video", False)),
    )

    # ── Class window + materials seen, side-by-side under the video ──
    materials = materials_from_xlsx or artefacts.get("materials_seen") or []
    window = parse_class_window(subject, session_id, v_path)

    m_col, w_col = st.columns([3, 2], gap="large")
    with w_col:
        st.markdown("##### Class window")
        if window:
            st.markdown(f"**Start:** `{window[0]}`")
            st.markdown(f"**End:** `{window[1]}`")
            st.caption(f"_{window[2]}_")
        else:
            st.markdown("—")

    with m_col:
        st.markdown(f"##### Materials seen ({len(materials)})")
        if not materials:
            if is_cloud_deploy():
                st.caption(
                    "_Materials list lives in the per-run JSON; available in "
                    "the local copy of the dashboard._"
                )
            else:
                st.caption(
                    "_Reasoner didn't emit `materials_seen` for this run "
                    "(older prompt version)._"
                )
        else:
            items = [str(m.get("item", "")).strip() for m in materials if m.get("item")]
            st.markdown(", ".join(items) if items else "—")

    st.markdown("---")

    # ── Section tabs: Environment / Content Knowledge / Facilitation / Warmth ──
    tabs = st.tabs([f"{SECTION_ICONS.get(s, '')} {s}" for s in SECTION_TABS])
    for tab, section_name in zip(tabs, SECTION_TABS):
        with tab:
            render_section_questions(qdf, section_name, video_state_key)


def components_html_scroll_to_anchor(anchor_id: str, *, do_scroll: bool = True) -> None:
    """Always-rendered zero-height JS injector. When `do_scroll` is True it
    scrolls Streamlit's parent document up to the element with the given id;
    when False it injects a no-op (kept so the widget tree stays stable
    across reruns — st.expander positional identity is fragile).
    """
    import streamlit.components.v1 as components
    if not do_scroll:
        components.html("<!-- noop -->", height=0)
        return
    components.html(
        f"""
        <script>
          const t = window.parent.document.getElementById("{anchor_id}");
          if (t) {{
            t.scrollIntoView({{behavior: "smooth", block: "start"}});
          }} else {{
            const c = window.parent.document.querySelector('section.main, [data-testid="stAppViewContainer"]');
            if (c) c.scrollTo({{top: 0, behavior: "smooth"}});
          }}
        </script>
        """,
        height=0,
    )


def render_section_questions(qdf: pd.DataFrame, section_name: str, video_state_key: str):
    section_df = qdf[qdf["section"] == section_name].copy()
    if section_df.empty:
        st.info(f"No questions tagged '{section_name}' for this run.")
        return

    def _qnum(qid):
        try:
            return int(str(qid).lstrip("Q"))
        except ValueError:
            return 9999
    section_df["_n"] = section_df["question_id"].map(_qnum)
    section_df = section_df.sort_values("_n")

    ans = int((~section_df["insufficient_information"]).sum())
    total = len(section_df)
    st.caption(f"{ans}/{total} answered · {total - ans} INSUFFICIENT")

    for _, row in section_df.iterrows():
        qid = row["question_id"]
        q_text = row.get("question_text") or ""
        ans_val = row.get("answer") or ""
        conf = str(row.get("confidence") or "").lower()
        ins = bool(row.get("insufficient_information"))
        ts_raw = row.get("evidence_timestamps") or ""
        ts_list = [t.strip() for t in str(ts_raw).split(",") if t.strip()] if ts_raw else []
        rationale = row.get("rationale") or ""

        conf_badge = {"high": "🟢", "medium": "🟡", "low": "🔴"}.get(conf, "⚪")
        chip = "❌ INSUFFICIENT" if ins else ans_val

        with st.expander(f"{qid} · {q_text}  —  {conf_badge} **{chip}**", expanded=False):
            cols = st.columns([3, 2])
            with cols[0]:
                st.markdown(f"**Answer:** {ans_val}")
                st.markdown(f"**Confidence:** {conf or '—'}")
                if rationale:
                    st.markdown(f"**Rationale:** {rationale}")
            with cols[1]:
                if ts_list:
                    st.markdown("**Evidence — tap to seek:**")
                    for ts in ts_list:
                        sec_off = hms_to_seconds(ts)
                        if st.button(
                            f"▶ {ts}",
                            key=f"jump_{video_state_key}_{qid}_{ts}",
                            help="Seek the video player above to this moment",
                        ):
                            st.session_state[video_state_key] = sec_off
                            st.session_state["_scroll_to_video"] = True
                            st.rerun()
                else:
                    st.markdown("_no evidence timestamps_")


if __name__ == "__main__":
    main()
