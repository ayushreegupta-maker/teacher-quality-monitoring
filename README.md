# Teacher Quality Monitoring (TQM)

Score Openhouse preschool classroom video against a rubric. Gemini watches the video and extracts evidence; Claude reads that evidence and answers ~32 rubric questions per session.

**Contact:** Ayushree Gupta — `ayushree.gupta@openhouse.study`

---

## Setup

Tested on macOS and Linux. ~5 minutes if your machine already has Python 3.10+ and `ffmpeg`.

### 1. Install prerequisites

```bash
# macOS
brew install python ffmpeg

# Ubuntu/Debian
sudo apt install python3 python3-venv ffmpeg
```

### 2. Clone and install

```bash
git clone https://github.com/ayushreegupta-maker/teacher-quality-monitoring.git
cd teacher-quality-monitoring

python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

### 3. Add API keys

Create a `.env` file in the repo root (gitignored, never committed):

```
GOOGLE_API_KEY=<your gemini api key>
ANTHROPIC_API_KEY=<your claude api key>
```

Both need paid billing. Free-tier quotas hit rate limits within one run.

### 4. Get the test video

The raw classroom recordings aren't in the repo (they're real children, and the files are large). To try the pipeline on a real session, **email Ayushree (`ayushree.gupta@openhouse.study`)** to get the test bundle for session `2026-05-18__D28__0900`.

Drop the three files she sends you into:

```
data/sessions/art/2026-05-18__D28__0900/
  0_segments_used.json
  2_boundaries.json
  3_trimmed.mp4    ← the 542 MB pre-trimmed video
```

---

## Run

One command. The recommended path is **Shape B**: Gemini extracts evidence in 22 parallel chunks, then Claude Opus reasons over that evidence.

```bash
.venv/bin/python scripts/run_rubric.py \
  --session-id 2026-05-18__D28__0900 \
  --rubric-version v2_2026-06-11 \
  --shape B \
  --vision-model gemini-3.5-flash \
  --activity-context "Today's planned segments: Art Games — learning about different textures. Art Gym — drawing lines in their sketchbook. Artiverse — making a collage. Experience Book — filling out reflections. Art Care — putting materials back."
```

| Stage | What runs | Time | Cost |
|---|---|---|---|
| Vision pass | 22 × Gemini chunks (concurrency 5) | ~2 min | ~$2.50 |
| Reasoner | 1 × Claude Opus 4.7 | ~30 sec | ~$0.20 |
| **Total** | | **~2.5 min** | **~$3** |

Re-runs with the same `--vision-model` and `--session-id` skip the vision pass (cached) and only re-run the reasoner. That's ~30 sec / ~$0.20 per re-score.

---

## Outputs

Every run writes to two places:

### 1. Per-run folder (`data/rubric_runs/`)

```
data/rubric_runs/art/2026-06-19T130300__v2_2026-06-11__claude-opus-4-7__B/
├── 0_config.json           ← run metadata (model, prompt hash, timestamps)
├── 4_rendered_prompt.txt   ← the exact text Claude saw
├── 5_answers.json          ← parsed answers per question (Q1–Q32):
│                             { id, answer, confidence, evidence_timestamps, rationale, ... }
└── 5_answers_raw.txt       ← Claude's raw response (in case parsing missed something)
```

### 2. Longitudinal answer log (`data/tqm_answers.xlsx`)

**Every run appends one row per question to this workbook** — the long-term record of every session ever scored, every Q&A response, every rationale.

Columns include: `session_id` · `subject` · `rubric_version` · `q_id` · `question` · `answer` · `confidence` · `evidence_timestamps` · `rationale` · `source_model` · `run_started_at` · `answer_type` · …

Use this for cross-session reporting, teacher comparisons, or quality trends over time. Open it in Excel / Numbers / Google Sheets and pivot however you need.

### 3. Cached evidence bundle (`data/evidence_cache/`)

The vision pass output (transcript + observations + phases + explanations + disturbances) is cached here so re-runs against the same video skip the expensive Gemini work. Cache key: `{session_id}__{vision_model}__fps-default__chunk-5min`.

You can also inspect this bundle directly — it's a single JSON file with everything Gemini extracted:

```
data/evidence_cache/art/<session_id>__<vision_model>__fps-default__chunk-5min/evidence_bundle.json
```

---

## Architecture in one screen

Two paths through the same rubric:

- **Shape A** — single Gemini call watches the whole trimmed video and answers all 32 rubric questions in one shot. Fast and cheap but evidence is whole-video spans; 1–4 scores tend to cluster at 4. Useful for prototyping. Not recommended for real scoring.

- **Shape B (recommended)** — two stages:

  ```
  Gemini (vision pass, chunked)              Claude Opus (reasoner)
  ┌─────────────────────────┐                ┌───────────────────────┐
  │ 22 × 5-min chunks       │   evidence     │ Reads everything as   │
  │ → transcript            │   bundle       │ text + the 32-Q       │
  │ → observations          ├───────────────►│ rubric. Returns       │
  │ → phases                │  (cached as    │ Q1–Q32 with answer +  │
  │ → explanations          │   JSON on      │ confidence + evidence │
  │ → disturbances          │   disk)        │ timestamps + rationale│
  └─────────────────────────┘                └───────────────────────┘
  ```

  Differentiated 1–4 scores. Time-anchored evidence ("at 01:10:23, child in red shirt said 'dirty hands'"). What we use in practice.

---

## Repo layout

```
adapters/         LLM SDK wrappers (Gemini, Claude, OpenAI)
pipeline/         Pipeline stages (session_video, boundaries, vision, evidence, rubric, …)
prompts/
  rubrics.xlsx        The rubric workbook — one tab per subject (art, robotics, …)
  vision.md           The Gemini vision-pass prompt (subject-conditional)
  art/                Art-class prompt templates (Shape A + Shape B per version)
scripts/
  run_rubric.py       Main entry point (see "Run" above)
  cctv_pull.py        Pull raw video from the NVR (Openhouse internal — VPN required)
  testing/            One-off diagnostic / probe scripts
data/                 All gitignored — recomputable from raw + scripts
  raw/                Raw NVR pulls (~1 GB per camera per day)
  sessions/           Per-session video cache (combined → boundaries → trimmed)
  evidence_cache/     Per-session vision output (cached JSON bundles)
  rubric_runs/        Per-run output folders (see "Outputs" above)
  tqm_answers.xlsx    Longitudinal log of every Q&A response across all runs
DECISIONS.md      Why we did things this way
PLAN.md           Architecture target + cheap-pilot rationale
```

---

## Common workflows

**Re-score with a different rubric version**
Change `--rubric-version` and re-run. Evidence cache is keyed on session + vision_model, not rubric version, so the bundle is reused. ~30 sec / ~$0.20.

**Try a different vision model**
Set `--vision-model gemini-3.5-flash` (default is `gemini-2.5-flash`). This routes to a fresh cache dir and triggers a full vision-pass rebuild. ~2.5 min / ~$3.

**Force a rebuild of an existing cached bundle**
Add `--force` (e.g. after tweaking the vision prompt).

**Add a new subject (e.g. robotics)**
1. Add a tab to `prompts/rubrics.xlsx`.
2. Author `prompts/<subject>/rubric_<subject>_<version>_shape_b.md` (copy from `prompts/art/`).
3. Optionally add a `{% if session.subject == "<subject>" %}` block to `prompts/vision.md` so the vision pass gets subject-specific phase types.

---

## Running on GitHub Codespaces (alternative)

Convenient for trying the pipeline without setting up locally.

1. On the repo page: **Code → Codespaces → Create codespace on main**.
2. In the Codespace terminal:
   ```
   python3 -m venv .venv && source .venv/bin/activate && pip install -e .
   ```
3. Add API keys as Codespace secrets: **Settings → Codespaces → Secrets** → `GOOGLE_API_KEY` and `ANTHROPIC_API_KEY`. They're auto-injected as env vars.
4. Upload the test-data files Ayushree sends you into `data/sessions/art/2026-05-18__D28__0900/` (the 542 MB video takes a few minutes to upload).
5. Run the same command as the "Run" section.

For sustained work, the local install is faster — Codespaces re-uploads the video every time you recreate the environment.

---

## Questions

Ayushree Gupta — `ayushree.gupta@openhouse.study`
