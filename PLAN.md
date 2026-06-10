# TQM — Consolidation & Cheap-Pilot Plan

**Status:** draft, awaiting your approval before any code changes.
**Author:** Claude session, 2026-06-09.
**Goal:** end the script-rewrite churn, line up a sub-$10 pilot that answers most of the rubric/settings questions on cached artifacts.

---

## 1. Repo audit

Inventory of every Python file + every prompt + every data directory, with a "what now?" tag.

### 1.1 Top-level Python (project root)

| File | Role | Status | Disposition |
|---|---|---|---|
| `batch_long_video.py` | Predecessor of `run_art_rubric_test.py`. Walks long videos in chunks, scores via old dimension model | **superseded** | **archive** to `scripts/_archive/` — keep for reference, do not delete |
| `batch_score.py` | Batch scoring runner using old 5-dim rubrics + DB writeback | **superseded** | **archive** to `scripts/_archive/` |

### 1.2 `scripts/`

| File | Role | Used by | Status | Disposition |
|---|---|---|---|---|
| `cctv_pull.py` | NVR → local raw .mp4 ingester | direct CLI | **current** | keep; small change in migration step 2 to land files into `data/raw/<subject>/<file>.mp4` instead of `data/raw/<file>.mp4` (subject already looked up from camera config) |
| `compare_models.py` | 84-combo runner: cached vision × N reasoners × M rubrics. Idempotent | direct CLI | **kept-but-stale** — works against OLD 5-dimension rubrics, not the new Q&A art rubric | keep as scaffold; refactor in §3 |
| `compare_models_report.py` | Builds quant + side-by-side report from `compare_models.py` outputs | downstream of `compare_models.py` | **stale** alongside its driver | refactor with §3 |
| `import_team_scores.py` | One-time XLSX → SQLite import of human rater scores | one-time | **completed** | keep — the import already populated `tqm.db`; no active dependency right now |
| `redo_transcript_dedupe.py` | One-off re-applier of the 3-pass dedupe to an existing `6_transcript.json` | direct CLI | **superseded** | **move into `pipeline/vision.py`** as a `redo_dedupe_for_run(run_dir)` function + `python -m pipeline.vision <run_dir>` entry point. Delete the script |
| `run_art_rubric_test.py` | The new 5-stage art rubric pipeline (combine → boundary → trim → rubric → report). 1061 lines | direct CLI | **current** — most recently iterated | **refactor in §3** into subject-agnostic runner |
| `run_question_bank.py` | 91-Q diagnostic bank runner | one-time | **completed** | keep as reference; not active |
| `run_spotlight_questions.py` | 13-Q focus-child spotlight runner | one-time | **completed** | keep as reference; not active |
| `score_art_with_claude.py` | One-shot Shape B test on the 2026-06-04 cached evidence | direct CLI | **current** (built today) | **promote** — basis for the cheap pilot §4 |

### 1.3 `pipeline/`

| File | Role | Used by | Status | Disposition |
|---|---|---|---|---|
| `boundaries.py` | Boundary detection wrapper around `prompts/detect_boundaries.md` | `run_art_rubric_test.py` | **current** | keep; update the `load_prompt(...)` callsite when the file is renamed to `prompts/boundaries.md` (migration step 3) |
| `extract.py` | Older artifact-extraction helpers | nothing live | **suspect-dead** | confirm no imports, then **archive** to `pipeline/_archive/` |
| `items.py` | Item/dimension data classes for old 5-dim rubric | `score.py`, `compare_models.py` | **stale** — old rubric shape | keep until §3 refactor lands; remove with old scorer |
| `render.py` | Jinja prompt rendering | `vision.py`, `boundaries.py` | **current** | keep |
| `score.py` | Per-dimension scoring (old rubric shape) | `compare_models.py` | **stale** | **refactor in §3** into Q&A scorer |
| `session_resolve.py` | Session metadata loader: NVR filename → camera + date → DB lookup → activity context | multiple | **current** | **rename to `session_context.py`** in migration step 4 (matches the function `resolve_session_context()` it exports). Future: rewire to read from a `teacher_schedule` table for per-hour, per-teacher resolution (§3.4, task #33). Known callsites to update at rename time: `pipeline/` imports + `tqm_db.py:160` + `batch_score.py:37` + `score_long_video.py:43` (all three move to `scripts/_archive/` in migration step 1 — update imports there too) |
| `types.py` | Pydantic models (SessionMeta, Transcript, etc.) | everywhere | **current** | keep — add new types as needed |
| `vision.py` | Chunked vision pass + 3-pass dedupe. Plan: absorb the standalone `redo_transcript_dedupe.py` as a public function + `__main__` entry | `run_art_rubric_test.py`, `compare_models.py` | **current** — just upgraded today | keep + extend with `redo_dedupe_for_run()` |

### 1.4 `adapters/`

| File | Role | Status | Disposition |
|---|---|---|---|
| `llm.py` | Single multi-provider adapter (Gemini files API, Anthropic, OpenAI) | **current** | keep; postpone Vertex AI rewrite (§3.6) |
| `db.py` | SQLite access for `tqm.db` | **current** | keep |
| `sessions.py` | `session_dir()` helper | **current** | keep |
| `retry.py` | tenacity-based retry decorator | **current** | keep |

### 1.5 `prompts/`

| File | Role | Status | Disposition |
|---|---|---|---|
| `detect_boundaries.md` | v0.7.1 — current boundary detection prompt | **current** | **rename to `boundaries.md`** in migration step 3 |
| `vision_observe.md` | Chunked vision pass prompt | **current** | **rename to `vision.md`** in migration step 3 |
| `score_dimension.md` | Old 5-dimension scoring prompt | **stale** | remove with §3 refactor |
| `consolidate_items.md` | Item-consolidation prompt for old flow | **suspect-dead** | confirm, then **archive** to `prompts/_archive/` |

The current art rubric prompt is built **inline inside `run_art_rubric_test.py`** rather than living in `prompts/`. That's part of why the prompt has been hard to iterate cleanly. §3 fixes this.

### 1.6 `data/`

| Subdir | Created by | Status | Disposition |
|---|---|---|---|
| `art_rubric_runs/` | `run_art_rubric_test.py` | **current** — 2 runs, the recent ones | keep |
| `raw/` | `cctv_pull.py` | **current** — 379 files, flat | **reorganise** in migration step 2: properly-named files → `data/raw/<subject>/`; older test files → `data/_legacy/raw_pre_subject_organization/` |
| `sessions/` | older `batch_*.py` | **legacy outputs** (90 dirs) | **archive** to `data/_legacy/sessions_batch_era/` — frees `data/sessions/` for the new session video cache (§3.5) |
| `segments/` | old chunking step | **legacy** | **archive** to `data/_legacy/segments/` |
| `batch_long_reports/`, `batch_reports/`, `long_video_reports/` | older runners | **legacy outputs** | **archive** to `data/_legacy/` |
| `question_bank/`, `question_bank_runs/` | `run_question_bank.py` | **completed, retainable** | keep — useful comparators |
| `spotlight_questions/`, `spotlight_runs/` | `run_spotlight_questions.py` | **completed, retainable** | keep — useful comparators |
| `cctv_cameras.xlsx` | manual | **current** | keep; eventually → SQLite (task #14) |
| `tqm.db` | SQLite | **current** | keep |

"Archive" means: move to `data/_legacy/` so it's out of the way but recoverable. Nothing deleted until you say so.

### 1.7 Other

| Path | Status | Disposition |
|---|---|---|
| `app/` | Web UI scaffold, unused | **suspect-dead** | confirm, then archive |
| `golden_set/` | Reference clips | **current** | keep |
| `DECISIONS.md` | Running log (54KB) | **current** | keep, add a "Plan adopted" entry |
| `.env`, `.env.example` | API keys | **current** | keep |
| `pyproject.toml` | Project config | **current** | keep |

---

## 2. The rubric workbook

One file at `~/Downloads/Teacher Quality Monitoring (1).xlsx` with three tabs.

| Tab | Questions | Sections | Cols | Maturity |
|---|---:|---:|---|---|
| Art | 31 | 4 | 5 (Criteria, Observe, Input ref, Analysis tag) | **complete** — has all the metadata the runner uses |
| Public Speaking | 32 | 4 | 3 (Criteria, Observe) | **draft** — no input refs, no Visual/Audio tags |
| Robotics | 30 | 4 | 3 (Criteria, Observe) | **draft** — same gap |

All three share the same 4-section structure: **Environment → Content Knowledge → Facilitation → Warmth**. The runner can treat sectioning as cross-subject.

**Implication for the runner:** PS + Robotics need their missing columns filled OR the runner needs to default missing fields (Analysis tag → "Visual+Audio"; Input ref → empty string and skip the interpolation). Default-fill is faster and lets us pilot all three subjects without manual rubric-completion work.

---

## 3. Target architecture

The single sentence: **one runner that takes a subject + rubric_version + model + shape, caches vision output, and writes auditable artifacts.**

### 3.1 Folder shape (proposed)

```
scripts/
  cctv_pull.py                ← unchanged
  run_rubric.py               ← NEW. Subject-agnostic. Replaces run_art_rubric_test.py

pipeline/
  rubric.py                   ← NEW. Single module exposing load_rubric() + render_prompt() + score().
                                 Replaces old pipeline/score.py
  vision.py                   ← unchanged code; absorbs redo_dedupe_for_run() as a public function
                                 with a `python -m pipeline.vision <run_dir>` entry point
  boundaries.py               ← unchanged (will load prompts/boundaries.md by its new name)
  types.py                    ← + Rubric, RubricSection, RubricQuestion, RubricAnswer, RubricAnswerSet, EvidenceBundle

prompts/
  boundaries.md               ← renamed from detect_boundaries.md
  vision.md                   ← renamed from vision_observe.md
  art/
    rubric_art_v1_<YYYY-MM-DD>.md       ← dated per revision. e.g. rubric_art_v1_2026-06-12.md
    rubric_art_v2_<YYYY-MM-DD>.md       ← next iteration with its own creation date
    rubric_art_v3_<YYYY-MM-DD>.md
  public_speaking/
    rubric_public_speaking_v1_<YYYY-MM-DD>.md
  robotics/
    rubric_robotics_v1_<YYYY-MM-DD>.md

data/
  raw/                       ← cctv_pull lands NVR-chunked segments here, bucketed by subject
    art/
      D28_hrbr_art_20260604_083132.mp4
      D28_hrbr_art_20260604_090132.mp4
      ...
    public_speaking/
      D14_hrbr_public_speaking_20260517_090348.mp4
      ...
    robotics/
      D29_hrbr_robotics_20260604_163627.mp4
      ...
  sessions/                  ← session video cache (§3.5). Built ONCE per session.
    art/<session_id>/        ← e.g. art/2026-06-04__D28__0900/
      0_segments_used.json   ← which raw segments were stitched + concat offsets
      1a_combined.mp4        ← full-res ffmpeg concat
      1b_boundary_input.mp4  ← downscaled, cheap input for boundary detection
      2_boundaries.json      ← class start/end wall-clock + offsets
      3_trimmed.mp4          ← cropped to class window
    public_speaking/<session_id>/
    robotics/<session_id>/
  evidence_cache/
    art/<session>__<vision_model>__<fps>__<chunking>/
      evidence_bundle.json   ← cached so re-runs with different rubrics cost $0
    public_speaking/...
    robotics/...
  rubric_runs/
    art/<ts>__<rubric_v>__<model>__<shape>/    ← one dir per rubric run
      0_config.json          ← what was run (subject implicit from path)
      4_evidence_bundle.json ← copy or symlink to cached bundle for audit
      5_answers.json         ← immutable per-run artifact (raw LLM output)
    public_speaking/<ts>__.../
    robotics/<ts>__.../

  tqm_answers.xlsx           ← ROLLING accumulator: all answers across all runs, one tab per subject
  tqm_answers.backup.xlsx    ← single safety copy; exists only while a merge is in progress or just failed
  _answer_queue/             ← transient: sidecar JSONs waiting to be folded into tqm_answers.xlsx
```

Session ID convention: `<YYYY-MM-DD>__<camera>__<HHMM>`, e.g. `2026-06-04__D28__0900`. Stable, sortable, human-readable. Subject lives in the parent folder, derived 1:1 from `camera_id` via `cctv_cameras.xlsx` (Openhouse rule: 1 camera = 1 room = 1 subject). See §3.5 for the full lifecycle.

### 3.2 The key cost trick: an evidence cache

Today every rubric tweak costs $20 + 75 minutes because we re-run vision. The fix:

- **Vision pass** is cached keyed on `(session_id, vision_model, fps, chunking)`. Produces `evidence_bundle.json` once. Lives in `data/evidence_cache/`.
- **Rubric pass** reads the bundle as JSON, runs the LLM call. Cheap (~$0.50), fast (~30s).
- Re-runs that vary only the rubric prompt or the reasoning model **never call vision again**.

This is what makes the pilot in §4 viable for <$10.

### 3.3 The rolling answer accumulator: `tqm_answers.xlsx`

Instead of N per-run CSVs scattered under `data/rubric_runs/`, there's ONE workbook at `data/tqm_answers.xlsx` that every run appends to. Five tabs:

| Tab | Purpose | Rows |
|---|---|---|
| `Art` | All art-class answers, every run, every config | One per (run × question) |
| `Public Speaking` | Same, PS sessions | One per (run × question) |
| `Robotics` | Same, robotics sessions | One per (run × question) |
| `Runs` | Audit log — one row per `run_rubric.py` invocation | One per run |
| `README` | Schema docs + write protocol + recovery procedure | Static text |

**Subject-tab schema (24 cols, identical across Art / Public Speaking / Robotics):**

| # | Column | Type | Example |
|---|---|---|---|
| 1 | `run_id` | timestamp | `2026-06-12T14:35:02` |
| 2 | `session_id` | string | `2026-06-04_122724` |
| 3 | `session_date` | date | `2026-06-04` |
| 4 | `camera` | string | `D28` |
| 5 | `teacher_id` | string | `T091` |
| 6 | `subject` | enum | `art` |
| 7 | `rubric_version` | string | `v1` |
| 8 | `vision_model` | string | `gemini-3.1-pro` |
| 9 | `vision_fps` | number | `0.5` |
| 10 | `chunking` | string | `5min` |
| 11 | `shape` | enum | `A` |
| 12 | `reasoner` | string | `claude-opus-4-7` |
| 13 | `run_n` | int | `1` |
| 14 | `question_id` | string | `Q5` |
| 15 | `section` | string | `Environment` |
| 16 | `question_text` | string | `# of minutes spent on warm up?` |
| 17 | `answer` | string | `6` |
| 18 | `confidence` | enum | `high` |
| 19 | `evidence_timestamps` | string | `00:02:25, 00:08:40` |
| 20 | `rationale` | string | `Texture exploration phase.` |
| 21 | `insufficient_information` | bool | `FALSE` |
| 22 | `had_evidence` | bool | `TRUE` |
| 23 | `evidence_parse_ok` | bool | `TRUE` |
| 24 | `prompt_hash` | string | `a3f7e2…` |

Uniqueness key: `(run_id, question_id)` within a tab. **Re-running the same config on the same session appends new rows with the next `run_n`** — full history is preserved so we can compute run-to-run consistency later.

**`Runs` tab schema (16 cols, audit log):**

`run_id`, `session_id`, `subject`, `config_slug` (e.g. `art__v1__gemini-3.1-pro__shapeA`), `rubric_version`, `vision_model`, `shape`, `reasoner`, `run_n`, `started_at`, `finished_at`, `wall_clock_seconds`, `questions_answered`, `questions_insufficient`, `prompt_hash`, `cost_usd_estimate`, `evidence_bundle_path`.

**The write protocol (corruption-proof):**

1. `run_rubric.py` finishes scoring → writes immutable per-run artifact at `data/rubric_runs/<ts>/<config>/5_answers.json`
2. Also writes a transient sidecar: `data/_answer_queue/<run_id>__<config>.json`
3. **Merge step** (runs automatically at end of `run_rubric.py`):
   - Copy `data/tqm_answers.xlsx` → `data/tqm_answers.backup.xlsx`
   - Open the XLSX, read all sidecars in `_answer_queue/`, append rows to the appropriate subject tab + 1 row per run to the `Runs` tab
   - Save to `tqm_answers.tmp.xlsx`
   - On success: rename tmp → main, delete the sidecars, **delete the backup**
   - On failure: discard tmp, leave the sidecars (next run retries the merge), **keep the backup** as the restore point

**Recovery rules of thumb:**

| State | Meaning | Action |
|---|---|---|
| No `tqm_answers.backup.xlsx` exists | Last merge succeeded; XLSX is clean | Nothing to do |
| `tqm_answers.backup.xlsx` exists but no merge is currently running | Last merge crashed mid-write; XLSX may be corrupt | `cp tqm_answers.backup.xlsx → tqm_answers.xlsx`, then re-run a merge to fold in any queued sidecars |
| XLSX and backup both unreadable | Total disaster | Rebuild accumulator by re-merging every `5_answers.json` under `data/rubric_runs/` (this is why we keep the per-run JSONs) |

**Why this design:**

- ONE file to open in Excel; pivot tables answer the comparison questions directly
- Per-run `5_answers.json` is the immutable ground truth — never overwritten, never deleted by the runner
- Sidecar queue means a slow/failed XLSX write never loses answers — they're durably on disk in `_answer_queue/` until merged
- Single backup, deleted on success — file system tells you at a glance whether the XLSX is healthy

### 3.4 Future state: `session_context` reads from a `teacher_schedule` table

`pipeline/session_context.py` (renamed from `session_resolve.py` in migration step 4) currently looks up `(camera_id, date) → activity_name` from the coarse `classroom_activity_assignments` table. That's good enough for "Monday is Art," but loses per-hour granularity, can't attribute scores to a specific teacher, and doesn't handle substitutions.

Future state — a richer schema sourced from each centre's actual schedule:

```
teacher_schedule
  centre              ← e.g. HRBR, Indiranagar
  camera_id           ← classroom camera
  starts_at           ← datetime (hourly precision, not daily)
  ends_at             ← datetime
  teacher_id          ← FK
  subject             ← art / public_speaking / robotics / …
  session_in_rotation ← e.g. "Week 3, Lesson 4"
  expected_duration_min
  lesson_plan_link    ← URL or path
  substitute_for      ← FK to the originally scheduled teacher_id (nullable)
```

Lookup becomes `(centre, camera_id, datetime) → row`, and `session_context` returns `teacher_id`, `teacher_name`, `subject`, `lesson_plan_link`, and the `substitute_for` chain.

**What this unlocks downstream:**
- Per-teacher dashboards (aggregate `tqm_answers.xlsx` by `teacher_id`)
- Correct attribution when a substitute teaches a class
- Hour-level resolution: same camera, two different classes per day, two different rubric tabs
- Schedule-vs-reality drift detection (scheduled 09:30 start vs detected 09:45 start)
- Lesson-plan-aware rubrics (eventually)

**Out of scope for the cheap pilot.** Tracked as task #33. Scheduled after pilot results land — depends on access to the centres' schedule sources (Google Sheets, central app, printed PDFs — TBD).

### 3.5 Raw input + session video cache (three cache layers, end to end)

**The input layer is bucketed by subject.** At Openhouse, 1 camera = 1 room = 1 subject (a stable fact carried in `cctv_cameras.xlsx`'s `subject` column — `art` / `public_speaking` / `robotics`). `cctv_pull.py` already encodes subject into every filename; the bucketing just promotes it to a path component so directory listings stay browseable as the corpus grows. `du -sh data/raw/art/` directly answers "how much art footage do we have?"

A class typically spans **multiple raw segments** because Hikvision chunks recordings every ~30 min. An art class from 09:00–10:30 might span 4 raw segments on the same camera.

Insert a `data/sessions/<subject>/<session_id>/` cache between `raw/` and `evidence_cache/`:

```
data/raw/<subject>/                       ← cctv_pull writes here; subject derived from camera config
    art/D28_hrbr_art_20260604_083132.mp4
    art/D28_hrbr_art_20260604_090132.mp4
    public_speaking/D14_hrbr_public_speaking_..._090348.mp4
    robotics/D29_hrbr_robotics_..._083000.mp4
        │
        ▼ session_context.py resolves
        │   (date, camera, time) → list of segment paths to stitch
        │   subject is implicit (lookup in cctv_cameras.xlsx)
        ▼

data/sessions/<subject>/<session_id>/     ← NEW. Built ONCE per session, reused
    art/2026-06-04__D28__0900/
        0_segments_used.json              ← which raw segments were stitched + offsets
        1a_combined.mp4                   ← full-res ffmpeg concat
        1b_boundary_input.mp4             ← downscaled, cheap input for boundary detection
        2_boundaries.json                 ← class start/end wall-clock + offsets
        3_trimmed.mp4                     ← cropped to the class window
        │
        ▼
data/evidence_cache/<subject>/<session>__<vision_model>__<fps>__<chunking>/
    evidence_bundle.json                  ← built ONCE per vision config (§3.2)
        │
        ▼
data/rubric_runs/<subject>/<ts>__<config>/  ← cheap, per-config (§3.1)
        │
        ▼
data/tqm_answers.xlsx                     ← rolling accumulator with per-subject tabs (§3.3)
```

**Three cache layers, each at a different cost tier:**

| Layer | What's cached | Built once per | Cost per build |
|---|---|---|---|
| `data/sessions/<session_id>/` | Stitched + trimmed video | Session (a class) | ~5 min ffmpeg + ~700MB disk |
| `data/evidence_cache/...` | Phases + transcript + observations | Session × vision config | ~75 min Gemini, ~$20 |
| `data/rubric_runs/<ts>/<config>/` | Rubric answers + report | Per rubric run | ~30 sec + ~$0.50 |

The further down, the cheaper. The further up, the more reusable. **Scoring the same session 5× with 5 different rubrics doesn't re-stitch the video, doesn't re-call Gemini for vision, doesn't re-detect boundaries.** All three caches keep their content.

**`session_id` naming convention:**

```
<YYYY-MM-DD>__<camera>__<HHMM>
```

Examples: `2026-06-04__D28__0900`, `2026-06-04__D28__1100`.

- Date + camera + class-start-time. All three are stable identifiers; none change after the class happened.
- ISO date prefix → sortable.
- Human-readable when you `ls data/sessions/`.
- Subject is **not** in the path — subject is metadata resolved by `session_context.py` at runtime. If a schedule revision later attributes the 09:00 D28 class to a different subject, the session dir is untouched.

**Subject handling end-to-end — subject is in the path AND in metadata:**

| Where subject is recorded | Source |
|---|---|
| `data/raw/<subject>/` folder | Derived from `cctv_cameras.xlsx` (camera → subject lookup) |
| `data/sessions/<subject>/<session_id>/` folder | Inherited from raw |
| `data/evidence_cache/<subject>/...` folder | Inherited from sessions |
| `data/rubric_runs/<subject>/...` folder | Inherited from sessions |
| Filename suffix (e.g. `D28_hrbr_art_…`) | Existing `cctv_pull` naming |
| `0_config.json` of each rubric run | Audit trail |
| `tqm_answers.xlsx` `subject` column | Tab + column for easy pivot |

`cctv_cameras.xlsx` is the single source of truth for camera → subject. A camera moving to a different subject is a config change + a one-time bulk move (rare).

**Handling multi-class days:** Since 1 camera = 1 subject, a camera can still host multiple classes per day, but they're all the same subject (e.g. D28 art-room hosts 09:00 cohort A, 11:00 cohort B, 14:00 cohort C). Each is a separate session: `data/sessions/art/2026-06-04__D28__0900/`, `…__1100/`, `…__1400/`. Each draws from its own slice of D28's raw segments for that day. Different cohorts/teachers are tracked at the session level via the teacher schedule (§3.4), not via folder structure.

**`session_context.py` gets one new responsibility:** given a session_id (or video file), it must also return the **list of raw segments that compose the class** + their concat offsets. Today that's done inline in `run_art_rubric_test.py`'s combine stage. Moving it into `session_context.py` consolidates session-related logic into one module.

### 3.6 Postponed: GCS / Vertex AI

We previously discussed routing video through GCS + Vertex AI for upload savings. Hold on this. The evidence-cache trick in §3.2 captures the bulk of the win (no re-uploads for rubric iteration). GCS is the right move *later* when we routinely re-run vision across many videos — not in this pilot.

### 3.7 Migration order

**Preflight (do these before step 1):**

- **P1. Free disk space.** Currently 21 GB free on 460 GB; the 9-day pull will need 50–100 GB. Move some of `data/raw/` to an external drive, prune unused derived caches, etc. Don't start migration step 1 until at least ~80 GB free.
- **P2. `git init` + initial commit.** Initialise version control at the project root. Existing `.gitignore` already excludes mp4s, db, venv, env — so the repo stays small (just code + docs). Single commit titled `Initial commit: TQM codebase pre-consolidation`. After this, every migration step ends with its own commit; reversal becomes `git revert` or `git checkout`.

**Migration steps:**

1. **Archive legacy first** to free namespaces. Move `data/sessions/` → `data/_legacy/sessions_batch_era/` (frees `data/sessions/` for the new session video cache). Move `data/segments/`, `data/batch_*_reports/`, `data/long_video_reports/` → `data/_legacy/`. Move `batch_long_video.py`, `batch_score.py`, `score_long_video.py`, `tqm_db.py` (top-level legacy utilities) → `scripts/_archive/`. Nothing deleted. Commit.
2. **Bucket `data/raw/` by subject** (one-time bulk move). New layout: `data/raw/art/`, `data/raw/public_speaking/`, `data/raw/robotics/`. Parse each existing properly-named file (`D14_hrbr_public_speaking_…`, `D28_hrbr_art_…`, `D29_hrbr_robotics_…`) for the subject token and move it. Older pre-subject test files (`D06_*.mp4`, `20250909_activity_*.mp4`, `test_video.mp4`, `playground/`) → `data/_legacy/raw_pre_subject_organization/`. Then **update `cctv_pull.py`** to write new files into `data/raw/<subject>/` going forward (one-line change — subject is already in scope at the write site). Commit. *(Note: mp4s are gitignored so the commit only captures the cctv_pull.py code change; the moves themselves are tracked via `migration_log.txt`, see §3.8.)*
3. **Rename + reorganise prompts.** `prompts/detect_boundaries.md` → `prompts/boundaries.md`. `prompts/vision_observe.md` → `prompts/vision.md`. Update the two `load_prompt("…")` callsites in `pipeline/boundaries.py` + `pipeline/vision.py`. Create empty `prompts/art/`, `prompts/public_speaking/`, `prompts/robotics/` folders. Move `prompts/score_dimension.md` + `prompts/consolidate_items.md` → `prompts/_archive/`.
4. **Rename `pipeline/session_resolve.py` → `pipeline/session_context.py`.** Update imports at every known callsite (already-archived files don't need updates). Add a new `resolve_session_segments(session_id) → list[Path] + offsets` function — moves the segment-stitching logic out of the legacy `run_art_rubric_test.py` into the session module.
5. **Build the session video cache layer** (§3.5). New code in `scripts/run_rubric.py` (or a helper module): given a session_id, fetch raw segments via `session_context`, ffmpeg-concat to `data/sessions/<subject>/<session_id>/1a_combined.mp4`, downscale to `1b_boundary_input.mp4`, run boundary detection → `2_boundaries.json`, trim to `3_trimmed.mp4`. Idempotent: skip if files already exist for that session_id.
6. **Build `pipeline/rubric.py`** with `load_rubric()` + the `Rubric` / `RubricSection` / `RubricQuestion` types added to `types.py`. Tested against all 3 tabs of the rubric workbook.
7. **Externalise the current inline art prompt** into `prompts/art/rubric_art_v1_<today>.md`. Verify it renders identically to today's output. **No behavior change** at this step.
8. **Add `render_prompt()` + `score()` to `pipeline/rubric.py`.** Build `scripts/run_rubric.py` end-to-end. Verify on the 2026-06-04 cached run: same input → same output as `run_art_rubric_test.py`.
9. **Add the evidence-cache layer** (§3.2). Verify Shape B reruns cost ~$0.
10. **Add the accumulator + merge step** (§3.3). Seed `tqm_answers.xlsx` from existing 2026-06-04 runs so the file is non-empty on first use.
11. **Absorb `scripts/redo_transcript_dedupe.py` into `pipeline/vision.py`** as `redo_dedupe_for_run(run_dir)` + a `__main__` entry. Archive the script to `scripts/_archive/`.
12. Run the pilot (§4).

Each step is small, reversible, and produces something usable before the next one starts.

### 3.8 Safety mechanism for the migration

Two redundant layers of safety so any step can be undone:

1. **Git (after preflight P2).** Each migration step commits when done. Reversal = `git revert <step-commit>` or `git checkout <step-commit>~1 -- <path>`. Standard git workflow.

2. **`migration_log.txt` at the project root.** Every file move (gitignored or not) appends a line `<ISO-timestamp>\t<old_path>\t<new_path>\tstep_<N>` after the `mv` succeeds. Survives a crash; never overwritten. Especially important for the data/raw/ reorganization (step 2) since mp4s are gitignored — git won't track those moves, but the log will.

3. **`scripts/reverse_migration.py` (built before step 1 runs).** Reads `migration_log.txt` and undoes any range of moves by inverting old↔new. Selective undo: `--step 2` reverses only step 2; `--since 'YYYY-MM-DD HH:MM'` reverses everything after that timestamp.

4. **All moves are same-filesystem.** Targets stay on the same volume as sources, so `mv` is atomic at the filesystem level. No in-progress half-moves.

5. **Dry-run first for each step.** The migration scripts print the full plan before any actual `mv`. You confirm; only then do moves happen.

---

## 4. The cheap pilot (~$5–10, ~1 hour wall-clock)

### 4.1 What we already have for free

The 2026-06-04_122724 run gave us:
- A clean evidence bundle (phases, explanations, deduped transcript, observations) for ~108 minutes of art class
- A Gemini-direct ("Shape A") set of 31 answers
- A first Claude-from-evidence ("Shape B") set of 31 answers

All of that is cached on disk. Re-running rubric prompts or different reasoners against this evidence costs only the reasoning call — a few cents per run.

### 4.2 What the pilot will answer

| Question | How |
|---|---|
| Does rubric version matter? | Run v1 / v2 / v3 prompts × cached evidence × same reasoner. Compare answers |
| Does the reasoner matter? | Same cached evidence × Claude vs Gemini-text vs (optional) GPT-5. Compare answers |
| Does Shape A beat Shape B? | We already have the comparison from today. Re-test with v2/v3 prompts to see if the answer flips |
| Coverage: which combo defers least? | % of INSUFFICIENT answers per combo (computed inline in the pilot report) |
| Auditability: which combo cites best? | % of answers with parseable evidence timestamps (computed inline in the pilot report) |

### 4.3 What the pilot will NOT answer

- Vision-model tradeoffs (Flash vs Pro) — needs fresh video calls, postponed
- FPS / chunking tradeoffs — same
- PS + Robotics behavior — needs fresh video on those subjects, postponed
- vs human raters — needs `team_scores` DB rows for this specific session (TBD if we have them)

### 4.4 Pilot matrix

3 rubric versions × 3 reasoners × 1 cached session = 9 reasoning calls.
×3 runs each for consistency = **27 calls**.
Cost: at ~$0.30–0.50 per call ≈ **$8–13 total**.

### 4.5 Pilot deliverable

A single Markdown report: `data/rubric_runs/<ts>/pilot_report.md` containing:
- Per-question agreement matrix across the 9 combos (built by pivoting `tqm_answers.xlsx` on `question_id` × `config_slug` — same shape as the side-by-side table we produced today for Shape A vs B on the 2026-06-04 run)
- Coverage % and rough auditability % per combo, computed inline in the report
- Top-3 disagreements with deep dives
- A recommended config for "what to run next" (the basis for whether to spend money on a vision-axis sweep at all)

For the pilot we'll compare configs using inter-combo agreement, coverage, and auditability — visible at a glance in `tqm_answers.xlsx`.

---

## 5. Decisions you need to make

| # | Decision | Status |
|---|---|---|
| D1 | Archive `sessions/`, `segments/`, `batch_*_reports/` to `data/_legacy/` | ✅ **approved** |
| D2 | ~~Delete~~ — instead, **archive** `batch_long_video.py`, `batch_score.py` (+ all other suspect-dead files: `extract.py`, `items.py`, old `score.py`, `score_dimension.md`, `consolidate_items.md`) | ✅ **approved (archive only, no deletion anywhere in the plan)** |
| D3 | §3 target architecture | ✅ **approved** |
| D4 | §3.7 migration order (now 11 steps) | ✅ **approved** |
| D5 | §4 pilot ($8–13 budget, 27 calls, ~1hr) | ✅ **approved** |
| D6 | PS + Robotics tabs: default-fill missing analysis tags as "Visual+Audio" | **awaiting OK** (default applied unless you push back) |
| D7 | Reasoner shortlist: Claude Opus 4.7 + Gemini 2.5 Pro text. Add GPT-5 as third? | **awaiting OK** (default: two reasoners) |
| D8 | Keep `compare_models.py` + `compare_models_report.py` as reference in `scripts/_archive/` after new runner is verified | ✅ **approved (archive only, never delete)** |

---

## 6. What I will NOT do until you OK this plan

- Delete or move any file
- Rewrite any prompt
- Make any Gemini call (cost zero)
- Touch `adapters/llm.py`
- Run the pilot

If you approve some items and not others, just tell me which. We'll proceed only on the green-lit ones.
