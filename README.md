# Teacher Quality Monitoring (TQM)

Score Openhouse preschool classroom video against a rubric using Gemini (vision pass) + Claude (reasoner).

The pipeline takes raw CCTV footage of a class, identifies when the class actually starts/ends, splits it into 5-minute chunks, has Gemini watch each chunk to produce a transcript + observations + phase log, and then has Claude read all that text and answer ~32 rubric questions per session.

---

## Quick start (local)

### 1. Prerequisites

- macOS or Linux
- Python ≥ 3.10
- `ffmpeg` (for video combine / trim) — `brew install ffmpeg` on macOS
- API keys: **Google (Gemini)** and **Anthropic (Claude)**. Both need a paid billing account; the free tier hits rate limits fast.

### 2. Clone + install

```bash
git clone https://github.com/ayushreegupta-maker/teacher-quality-monitoring.git
cd teacher-quality-monitoring

python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

### 3. Provide API keys

Create a `.env` in the repo root (gitignored):

```
GOOGLE_API_KEY=<your gemini api key>
ANTHROPIC_API_KEY=<your claude api key>
```

Optionally override the default models:

```
VISION_MODEL=gemini-3.5-flash      # default: gemini-2.5-flash
```

### 4. Get test data

The raw CCTV recordings and a pre-trimmed test video aren't in the repo (they're large and contain real classroom footage). To run on a real session, **reach out to Lxd (lxd@openhouse.study)** for access to the trimmed test video for `2026-05-18__D28__0900`.

Place the trimmed video at:

```
data/sessions/art/2026-05-18__D28__0900/3_trimmed.mp4
```

Along with these companion files (Lxd will share them together):

```
data/sessions/art/2026-05-18__D28__0900/0_segments_used.json
data/sessions/art/2026-05-18__D28__0900/2_boundaries.json
```

### 5. Run

Recommended: **Shape B** (Gemini extracts evidence in parallel chunks; Claude Opus answers the rubric from that evidence).

```bash
.venv/bin/python scripts/run_rubric.py \
  --session-id 2026-05-18__D28__0900 \
  --rubric-version v2_2026-06-11 \
  --shape B \
  --vision-model gemini-3.5-flash \
  --activity-context "Today's planned segments: Art Games — learning about different textures. Art Gym — drawing lines in their sketchbook. Artiverse — making a collage. Experience Book — filling out reflections. Art Care — putting materials back."
```

Wall-clock: ~2.5 min (vision pass parallelised across 5 concurrent Gemini calls + one Claude Opus call). Cost: ~$3.

Output:

```
data/rubric_runs/art/<timestamp>__v2_2026-06-11__claude-opus-4-7__B/
  0_config.json
  4_rendered_prompt.txt    # the exact text Claude saw
  5_answers.json           # parsed Q1–Q32 with confidence + evidence
  5_answers_raw.txt        # Claude's raw response
```

Evidence bundle (cached, reused across rubric versions):

```
data/evidence_cache/art/<session_id>__<vision_model>__fps-default__chunk-5min/
  evidence_bundle.json     # transcript + observations + phases + explanations + disturbances
```

---

## Running on GitHub Codespaces (alternative)

If you'd rather not set up locally:

1. From the repo page on GitHub: **Code → Codespaces → Create codespace on main**.
2. Once the VM is ready, open a terminal and:
   ```
   python3 -m venv .venv && source .venv/bin/activate && pip install -e .
   ```
3. In **Settings → Codespaces → Secrets** (per-repo), add `GOOGLE_API_KEY` and `ANTHROPIC_API_KEY`. They're injected into the Codespace as env vars automatically.
4. Get test data from Lxd (see step 4 above) and upload it to the Codespace at the same path. The trimmed video is ~540 MB — Codespace storage handles this fine but uploads take a few minutes.
5. Run the same command as the local section.

Codespaces is convenient for one-off experiments. For sustained work the local install is faster (no upload bottleneck on the video).

---

## Architecture

Two end-to-end paths through the same rubric:

- **Shape A** — single Gemini call watches the trimmed video and answers all rubric questions in one shot. Fast (~$0.50/run) but evidence is often whole-video spans and 1–4 scores cluster at 4. Built for prototyping; not recommended for real scoring.

- **Shape B (recommended)** — two-stage:
  - **Vision pass** (Gemini, chunked 5-min × 22). Produces transcript + observations + `phases` + `explanations` + `disturbances`. Runs at concurrency 5; ~2 min wall-clock. Cached in `data/evidence_cache/`.
  - **Reasoner pass** (Claude Opus 4.7). Reads the cached evidence as text + the 32-question rubric, returns answers with `confidence` (high/medium/low) and `evidence_timestamps`. ~30 sec.

Shape B gives differentiated 1–4 scores and time-anchored evidence ("at 01:10:23, child in red shirt said 'dirty hands'"); Shape A doesn't.

---

## Layout

```
adapters/         # LLM SDK wrappers (Gemini + Claude + OpenAI)
pipeline/         # Pipeline stages (session_video, boundaries, vision, rubric, evidence, …)
prompts/          # All prompts. The rubric is in prompts/rubrics.xlsx (one tab per subject).
  art/            # Per-subject Shape A + Shape B prompt templates
  vision.md       # The vision-pass prompt (subject-conditional for phases/explanations)
scripts/
  run_rubric.py   # Main entry point — see Quick start above
  cctv_pull.py    # Pull raw video from NVR (Openhouse internal — VPN required)
  testing/        # One-off diagnostic / probe scripts; delete-able
data/             # All gitignored — recomputable from raw + scripts
  raw/            # Raw NVR pulls (often ~1 GB per camera per day)
  sessions/       # Per-session video cache (combined → boundaries → trimmed)
  evidence_cache/ # Per-session vision-pass output (recomputable, cheap to keep)
  rubric_runs/    # Per-run output (config + rendered prompt + answers)
DECISIONS.md      # Why we did things this way
PLAN.md           # Architecture target + cheap pilot rationale
```

---

## Common workflows

**Just want to re-score with a different rubric version?** No need to re-run the vision pass — the evidence cache is keyed on session + vision_model, not rubric version. Change `--rubric-version` and re-run; the existing bundle is reused. ~30 sec, ~$0.20.

**Switching vision model invalidates the evidence cache** (cache key includes `vision_model`). The next run rebuilds the bundle.

**Run a single chunk for debugging:** uncomment the `chunks = chunks[:1]` line in `pipeline/vision.py:vision_observe` — no permanent CLI flag yet.

**Add a new subject (e.g. robotics):**
1. Add a tab to `prompts/rubrics.xlsx`.
2. Author `prompts/<subject>/rubric_<subject>_<version>_shape_b.md` (copy from `prompts/art/`).
3. Optionally extend `prompts/vision.md` with a `{% if session.subject == "<subject>" %}` block to inject subject-specific phase types.

---

## Questions / access

Contact Lxd (lxd@openhouse.study).
