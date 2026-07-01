# Teacher Quality Monitoring (TQM)

Score Openhouse preschool and primary classroom videos against a subject-specific rubric. Gemini watches the video and extracts evidence; Claude reads that evidence and answers 32–34 rubric questions per session. A live Streamlit dashboard lets the quality + training teams walk every scored session and act on it.

**Subjects live today:** Art (3–5), Public Speaking (5–8), Robotics (5–8).

**Contact:** Ayushree Gupta — `ayushree.gupta@openhouse.study`

---

## Live dashboard

**URL:** <https://teacher-quality-monitoring.streamlit.app/>

Password-gated. **Message Ayushree (`ayushree.gupta@openhouse.study`) for the password.**

Two views in the app:

1. **Sessions** — pick a canonical run, watch the trimmed class video, and walk the Q&A section-by-section (Environment / Content Knowledge / Facilitation / Warmth). Every answer shows confidence, cited timestamps, and rationale — click a timestamp to seek the video to that moment.
2. **Coaching Queue** — sessions the rules flagged for the training team's attention. Marks any session where a scored `1–4` question came out `1` or `2` with medium/high confidence, or any safety-themed yes/no question came back `Yes`. Set a decision (immediate training / training required / no training required) plus notes, saved to Supabase.

---

## What runs where

```
┌──────────────────────────────┐
│  Local machine (Mac)         │
│  ─────────────────────────── │
│  scripts/run_rubric.py       │
│    · combines raw NVR clips  │
│    · Gemini vision pass      │
│    · Claude Opus reasoner    │
│    · writes to Postgres      │
└──────────┬───────────────────┘
           │ upsert
           ▼
     ┌──────────────┐         ┌──────────────────┐
     │  Supabase    │◀─read───│  Streamlit Cloud │
     │  (Postgres)  │         │  frontend/       │
     │   runs       │         │  quality_app.py  │
     │   answers    │         └─────────┬────────┘
     │   coaching_  │                   │ signed URL
     │   actions    │                   ▼
     └──────────────┘         ┌──────────────────┐
                              │  Cloudflare R2   │
                              │  (video hosting) │
                              └──────────────────┘
```

The pipeline runs on a local Mac (needs API keys + `ffmpeg` + disk for raw video). Everything read by the dashboard lives in Supabase; the videos it plays live in Cloudflare R2.

---

## Setup (for running the pipeline)

Tested on macOS. ~10 minutes if Python 3.10+ and `ffmpeg` are already installed.

### 1. Install prerequisites

```bash
brew install python ffmpeg
```

### 2. Clone and install

```bash
git clone https://github.com/ayushreegupta-maker/teacher-quality-monitoring.git
cd teacher-quality-monitoring

python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

### 3. Add credentials to `.env`

Create a `.env` file at the repo root (gitignored, never committed):

```bash
# Model providers (both need paid billing)
GOOGLE_API_KEY=<your Gemini API key>
ANTHROPIC_API_KEY=<your Claude API key>

# Supabase — where every scored run lands
SUPABASE_URL=https://<project-id>.supabase.co
SUPABASE_SERVICE_KEY=<service_role key>

# Cloudflare R2 — where trimmed videos are uploaded (for dashboard playback)
R2_ACCOUNT_ID=<your Cloudflare account id>
R2_BUCKET=openhouse-tqm-videos
R2_ACCESS_KEY_ID=<from your R2 API token>
R2_SECRET_ACCESS_KEY=<from your R2 API token>
```

Ask Ayushree for the shared credentials if you're inside Openhouse.

### 4. Get the test video

Raw classroom recordings aren't in the repo (real children + huge files). Email Ayushree for the test bundle for session `2026-05-18__D28__0900` (Art class). Drop the three files into:

```
data/sessions/art/2026-05-18__D28__0900/
  0_segments_used.json
  2_boundaries.json
  3_trimmed.mp4    ← the 542 MB pre-trimmed video
```

---

## Run the pipeline

Score one session end-to-end (fast path — assumes `3_trimmed.mp4` already exists):

```bash
.venv/bin/python scripts/run_rubric.py \
  --trimmed-video data/sessions/art/2026-05-18__D28__0900/3_trimmed.mp4 \
  --rubric-version v2_2026-06-11 \
  --shape B \
  --activity-context "Today's planned segments: Art Games — textures. Art Gym — lines. Artiverse — collage. Experience Book — reflections."
```

| Stage | What runs | Time | Cost |
|---|---|---|---|
| Vision pass | ~22 × Gemini chunks (concurrency 5) | ~2 min | ~$2.50 |
| Reasoner | 1 × Claude Opus 4.7 | ~30 sec | ~$0.20 |
| Supabase write | 1 run row + 32 answer rows upserted | <1 sec | — |
| **Total** | | **~2.5 min** | **~$3** |

Re-runs with the same `--vision-model` and session skip the vision pass (cached to `data/evidence_cache/`) and only re-run the reasoner. ~30 sec / ~$0.20 per re-score.

**Full pipeline** — starts from raw NVR clips instead of a pre-trimmed video:

```bash
.venv/bin/python scripts/run_rubric.py \
  --session-id 2026-05-18__D28__0900 \
  --rubric-version v2_2026-06-11 \
  --shape B \
  --activity-context "..."
```

This adds a `combine → detect boundaries → trim` step (~5–10 min extra) that produces the `3_trimmed.mp4` from the raw camera clips in `data/raw/<subject>/`.

---

## Where each run's data ends up

| Artefact | Location | Committed? |
|------------------|---|---|
| `runs` table (one row per rubric run) | Supabase Postgres | data lives in the DB |
| `answers` table (one row per Q × run) | Supabase Postgres | data lives in the DB |
| `coaching_actions` (per-session training decision + notes) | Supabase Postgres | data lives in the DB |
| Trimmed class video (`3_trimmed.mp4`) | Local disk during dev; Cloudflare R2 for the deployed dashboard | ❌ (large + children's faces) |
| Vision-pass cache (transcript / observations / phases) | `data/evidence_cache/<subject>/<session>__<model>__…/evidence_bundle.json` | ❌ (rebuildable, ~1 MB each) |
| Per-run audit trail (0_config.json, 5_answers.json, raw prompt + response) | `data/rubric_runs/<subject>/<config_slug>/` | ❌ (local audit only) |
| Answer-queue sidecar (durable buffer between run + Supabase upsert) | `data/_answer_queue/*.json` (deleted on successful upsert) | ❌ |

---

## Two paths through the same rubric

- **Shape A** — single Gemini call watches the whole trimmed video and answers all 32 rubric questions in one shot. Fast and cheap but evidence is whole-video spans; 1–4 scores tend to cluster at 4. Useful for prototyping. Not recommended for real scoring.

- **Shape B (recommended)** — two stages:

  ```
  Gemini (vision pass, chunked)              Claude Opus (reasoner)
  ┌─────────────────────────┐                ┌───────────────────────┐
  │ N × 5-min chunks        │   evidence     │ Reads everything as   │
  │ → transcript            │   bundle       │ text + the 32/34-Q    │
  │ → observations          ├───────────────►│ rubric. Returns       │
  │ → phases                │  (cached as    │ Q1-QN with answer +   │
  │ → explanations          │   JSON on      │ confidence + evidence │
  │ → disturbances          │   disk)        │ timestamps + rationale│
  │ → materials list        │                │ + materials_seen list │
  └─────────────────────────┘                └───────────────────────┘
  ```

  Differentiated 1–4 scores. Time-anchored evidence ("at 01:10:23, child in red shirt said 'dirty hands'"). What we use in practice.

Every Shape B run also emits a **`materials_seen`** list — a deduplicated inventory of teaching materials Gemini spotted (blocks, lever apparatus, drawing paper, motors, etc.). Surfaces in the dashboard next to the class video.

---

## Repo layout

```
adapters/         LLM SDK wrappers (Gemini, Claude)
pipeline/         Pipeline stages
  session_video.py     combine + boundary-detect + trim raw NVR clips
  vision.py            chunked Gemini call → evidence bundle
  evidence.py          bundle cache
  rubric.py            Claude Opus call over the bundle
  answers_book.py      sidecar → Supabase upsert
  types.py             pydantic models (RubricAnswerSet, MaterialSeen, ...)

prompts/
  rubrics.xlsx         The rubric workbook — one tab per subject (Art / Public Speaking / Robotics)
  vision.md            The Gemini vision-pass prompt (subject-conditional)
  art/                 Shape B prompt templates for Art (v1 + v2)
  public_speaking/     Shape B prompt template for PS
  robotics/            Shape B prompt template for Robotics

frontend/         Streamlit quality dashboard
  quality_app.py       single-page app: Sessions + Coaching Queue views
  DEPLOY.md            step-by-step Streamlit Cloud deploy recipe

scripts/
  run_rubric.py                     main entry point (see "Run" above)
  cctv_pull.py                      pull raw video from the NVR (Openhouse internal — VPN required)
  upload_videos_to_r2.py            reusable helper: push trimmed videos to Cloudflare R2
  testing/                          diagnostic / probe scripts

data/             All gitignored — recomputable from raw + scripts
  raw/                Raw NVR pulls (~1 GB per camera per day)
  sessions/           Per-session video cache (combined → boundaries → trimmed)
  evidence_cache/     Per-session vision output (cached JSON bundles)
  rubric_runs/        Per-run audit output (see table above)
  _answer_queue/      Sidecar queue for durable Supabase writes

DECISIONS.md      Why we did things this way
PLAN.md           Architecture target + cheap-pilot rationale
```

---

## Common workflows

**Re-score with a different rubric version.** Change `--rubric-version` and re-run. Evidence cache is keyed on session + vision_model, not rubric version, so the bundle is reused. ~30 sec / ~$0.20.

**Try a different vision model.** Set `--vision-model gemini-3.5-flash` (default is `gemini-2.5-flash`). This routes to a fresh cache dir and triggers a full vision-pass rebuild. ~2.5 min / ~$3.

**Force a rebuild of an existing cached bundle.** Add `--force` (e.g. after tweaking the vision prompt).

**Add a new subject** (e.g. Language)
1. Add a tab to `prompts/rubrics.xlsx`.
2. Author `prompts/<subject>/rubric_<subject>_<version>_shape_b.md` (copy from `prompts/art/`).
3. Add an `{% elif session.subject == "<subject>" %}` branch to `prompts/vision.md` so the vision pass emits subject-specific phase types.
4. Add the camera → subject mapping in `data/cctv_cameras.xlsx`.

**Deploy the dashboard.** See `frontend/DEPLOY.md` — Streamlit Community Cloud recipe with the password gate + R2 + Supabase secrets you need in the Cloud settings panel.

---

## Questions

Ayushree Gupta — `ayushree.gupta@openhouse.study`
