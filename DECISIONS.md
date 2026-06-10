# TQM — Decisions & Next Steps

Living document. Update whenever a meaningful decision is made or a phase boundary is crossed. The point is that anyone reading this in 3 months (or a new collaborator joining) can understand *why* the system is the way it is, not just *what* it does.

Last updated: **2026-06-10**

---

## 2026-06-10 — Dead-code sweep across live modules

Audited every top-level symbol in the live `adapters/` and `pipeline/` modules for live callers. Found ~830 lines of dead code (~56% of the audited surface) once the legacy 5-dimension scoring path was retired. All dead chunks **archived, not deleted** — recoverable via either the live `_archive/` packages or git history.

**Files trimmed:**

| File | Before | After | What came out |
|---|---:|---:|---|
| `adapters/db.py` → `adapters/_archive/db.py` | 561 | 0 (gone) | All 22 functions. The `team_scores` table data stays in `tqm.db`; only the I/O wrappers were dead. Likely revived when task #33 (teacher_schedule) lands |
| `adapters/llm.py` | 443 | 244 | 5 dead LLMAdapter methods → `adapters/_archive/llm_legacy_methods.py` as `LLMAdapterLegacy` (subclass). Kept: `call_claude_text`, `upload_video`, `call_gemini_video`. Dropped: `call_claude_json`, `call_openai_text`, `call_openai_json`, `call_gemini_text`, `call_gemini_text_json` |
| `pipeline/render.py` | 89 | 51 | `load_rubric` (YAML), `render_transcript`, `render_visual`, `render_score_prompt` → `pipeline/_archive/render_legacy.py` |
| `pipeline/session_context.py` | 396 | 153 | `parse_camera_and_recorded_at`, `resolve_session_context`, `_activity_by_name`, `_activity_id_by_name` → `pipeline/_archive/session_context_legacy.py` |
| `pipeline/types.py` | 285 | 232 | `LegacyRubric*` Pydantic types (3 classes) → `pipeline/_archive/legacy_rubric_types.py` |

**Recovery patterns:**

- **Need an OpenAI / Claude JSON / Gemini-text caller?** Change `LLMAdapter()` → `LLMAdapterLegacy()` at the instantiation site (`from adapters._archive.llm_legacy_methods import LLMAdapterLegacy`). Subclass re-adds all 5 methods.
- **Task #33 wants the legacy DB-backed session context?** Import directly: `from pipeline._archive.session_context_legacy import resolve_session_context`. The function works as-is; only the import path changes. Its DB layer is `adapters._archive.db`.
- **Need the YAML 5-dim rubric loader?** `from pipeline._archive.render_legacy import load_rubric, render_score_prompt`. Types come from `pipeline._archive.legacy_rubric_types`.

**Why archive instead of delete:** consistent with the broader `_archive/` discipline. Git would preserve the bodies too, but importable archive modules let us re-enable a code path with a one-line import change rather than a `git show` + paste.

**What's NOT in the dead-code sweep:** the `_archive/` packages aren't imported by anything live. They're inert until someone explicitly references them.

### 2026-06-10 — Round 2 (second sweep across remaining modules)

After the first sweep I'd only audited 5 of 16 files. Did the full inventory and found a meaningful second tranche:

| File | Lines saved | What came out |
|---|---:|---|
| `pipeline/types.py` (2nd pass) | −53 | 7 legacy-scoring/items types: `Evidence`, `ScoreValue`, `DimensionScore`, `SessionScores`, `ItemEntry`, `OtherItem`, `ConsolidatedItems` → `pipeline/_archive/legacy_score_types.py`. Also dropped unused `Union` import |
| `adapters/sessions.py` | 34 → 13 | `register_session`, `load_session`, `list_sessions` → `adapters/_archive/sessions_legacy.py`. Kept `session_dir` (6 callers) |
| `pipeline/boundaries.py` | 172 → 105 | The public `detect_boundaries(session, llm)` was archived to `pipeline/_archive/boundaries_legacy.py` — it was orphaned when `session_video.stage2_detect_boundaries` inlined the logic to pass `fps=0.3`. Internal helpers (`_parse_hms`, `_format_hms`, `_subtract_clocks`, `_derive_elapsed_from_walls`) stayed live because session_video imports them directly |
| `pipeline/evidence.py` | −44 | `enrich_bundle_with_shape_a` → `pipeline/_archive/evidence_legacy.py`. Designed for a future Shape A → enrich → Shape B flow that isn't wired up. Restore when wiring it |
| `scripts/run_rubric.py` | −1 | Removed dead import of `enrich_bundle_with_shape_a` |

**Recovery patterns (round 2):**

- **Legacy scoring/items types** for the archived 5-dim flow: `from pipeline._archive.legacy_score_types import DimensionScore, ConsolidatedItems, ...`
- **Session meta.json persistence** (register/load/list sessions): `from adapters._archive.sessions_legacy import register_session, load_session, list_sessions`
- **Old whole-video boundary call** (without fps-capping): `from pipeline._archive.boundaries_legacy import detect_boundaries`
- **Shape A → bundle enrichment** for the future Shape A → enrich → Shape B flow: `from pipeline._archive.evidence_legacy import enrich_bundle_with_shape_a`

**Combined sweep total:** ~1,400 lines stripped from live modules across both rounds. Verified: full live pipeline + all 8 archived modules + 4 entry-point scripts import clean.

---

## Vision

Score teacher effectiveness in classrooms from CCTV video + mic audio, against a rubric. Output per-session per-dimension scores with grounded evidence, and surface specific, actionable nudges to teachers.

Three audiences:
- **Teachers** — self-reflection, weekly nudges, growth tracking
- **School admins / coaches** — roll-up view, drill into sessions, coaching prep
- **Internal R&D** — rubric design, prompt calibration, model improvement

Long-term destination includes an **interactive coaching agent** that teachers can chat with about their sessions ("show me a moment I did questioning well", "why did engagement drop this week"). That feature is Phase 2; the data model and pipeline are designed to make it cheap to build later.

---

## Phasing

| Phase | Scope | Status |
|---|---|---|
| **0a** | Terminal-only scoring pipeline on existing test videos. Calibrate rubric + prompts against a small human-scored golden set. | **In progress** |
| **0b** | Minimal FastAPI + SQLite + 3-page UI (upload / session list / session detail with click-to-seek evidence). For internal team dogfooding. | Not started |
| **1** | Pilot in 1–5 classrooms. Real ingestion, real teachers seeing scores. Add team disagreement capture for calibration. | Not started |
| **2** | School-wide rollout. Coaching agent. Multi-school data model. Scheduled NVR pulls. | Not started |
| **3** | Multi-school SaaS. Multi-tenancy, billing, ops scaling. | Not started |

---

## Decisions made

| Date | Decision | Why |
|---|---|---|
| 2026-05-14 | Build a separate codebase from teammate's parent-update repo (`nikhil2197/agentic_vid_QA`); use it as reference only | Different problem shape: fixed-rubric scoring vs. free-form Q&A. Reuse adapter patterns, not the architecture. |
| 2026-05-14 | Plain async Python pipeline. **No LangGraph.** | Scoring is a linear pipeline with one fan-out point; LangGraph solves agentic branching that we don't have. At scale, the right tool is a workflow engine (Inngest/Temporal), not LangGraph. LangGraph reserved for the future coaching agent feature. |
| 2026-05-14 | Per-dimension **isolated** scoring first; graduate to a bundled prompt later, dimension by dimension | Diagnose dimension quality before optimizing for cost. Graduation criteria: agreement κ ≥ 0.7 against human golden set + stable prompt for 10 sessions + ±0.5 score shift max when bundled. |
| 2026-05-14 | Phase 0a is terminal-only; Phase 0b adds light DB + minimal 3-page UI | Don't slow down rubric/prompt iteration with infrastructure. UI exists only after scoring is calibrated. |
| 2026-05-15 | **Rubric v0.1**: Pre-K CLASS structure (3 domains, 10 dimensions) with our own anchor language and signal mappings | CLASS is widely used in research; the structure (domain names, dimension names, indicator names) is publicly described in academic literature. Anchor descriptions are original to avoid Teachstone IP entanglement. License CLASS from Teachstone if commercializing under a "CLASS-based" label. |
| 2026-05-15 | **1–5 scoring scale** (collapsed from CLASS 1–7) | Finer granularity than the AI scorer can reliably distinguish at this stage. Easier to widen later than narrow. |
| 2026-05-15 | Negative Climate aligned so **5 always means "good"** across all dimensions | Cleaner UI semantics. Deviates from CLASS convention where high Negative Climate = more negativity. |
| 2026-05-15 | **Models**: Claude Sonnet 4.6 for scoring, Gemini 2.5 Flash for video understanding | Mixed providers, play to each one's strengths. Claude is strong at structured-JSON-against-rubric; Gemini natively ingests video. |
| 2026-05-15 | **Skip GCS for Phase 0a.** Use Gemini Files API with local video paths. | Faster iteration. Commit to GCS bucket layout in Phase 1 (matches teammate's pattern). |
| 2026-05-15 | Every scored output stores `prompt_hash` + `rubric_version` | Required for the future coaching agent ("explain this score") and for calibration debugging. Cheap now, painful to retrofit. |
| 2026-05-15 | Evidence in scores must include `ts_start` + `ts_end` (a *range*, not a point) | The Phase 0b UI's click-to-seek and the Phase 2 coaching agent both need ranges to render clips. |
| 2026-05-15 | Multi-tenancy: every artifact carries a `school_id` (always `"default"` for now) | Multi-school is Phase 3 product but multi-tenant *data model* costs nothing to add now. |
| 2026-05-18 | **Vision pass disables Gemini 2.5 "thinking" tokens** (`thinking_budget=0`) and forces `response_mime_type=application/json` | Thinking tokens count against `max_output_tokens` and silently consumed the budget, returning ~6 KB instead of expected ~150 KB. Mechanical transcription doesn't need reasoning. |
| 2026-05-18 | **Vision prompt v0.2**: observations come first in schema; transcript is compact (merge consecutive same-speaker turns, no per-second segments, cap verbatim repetitions) | If output truncates, observations (small, ~5–10 KB) are preserved; transcript (large, variable) takes the hit. Transcript-driven dimensions degrade gracefully rather than the whole pipeline failing. |
| 2026-05-18 | **`json_repair` fallback** in `parse_json_lenient` when Gemini output is truncated | Recovers the valid JSON prefix instead of erroring; missing keys default to empty arrays so scoring still proceeds. |
| 2026-05-18 | **Chunked vision processing**: vision pass splits long videos into 5-min chunks via Gemini `video_metadata` offsets, processes each separately, shifts timestamps in Python, merges results | Single-call vision pass on long videos hits the 65 K output token cap and truncates the transcript. 5-min chunks comfortably fit complete output per call. Same total input cost (video is re-referenced not re-uploaded), full transcript preserved. |
| 2026-06-01 | **Tailscale subnet router** on a Windows laptop at each centre for remote NVR access | iVMS-4200's download is disabled for Cloud P2P-registered NVRs; web UI download needs a Windows-only ActiveX plugin; port-forwarding the NVR exposes a heavily targeted admin interface to the public internet. Tailscale gets us LAN reach over the existing internet line with end-to-end encryption and no exposed ports. |
| 2026-06-01 | **ISAPI over HTTP digest auth** (not the SDK on port 8000, not iVMS-4200) for search + download | ISAPI is reliable from `curl`/Python over any internet path, runs on port 80 (already exposed to the LAN for the web UI), and doesn't depend on Hikvision's flaky Mac client. iVMS-4200 Mac is the most fragile link in the Hikvision stack — every shortcut through it backfired. |
| 2026-06-02 | **Auto-transcode every downloaded segment** to H.264 + AAC inside `cctv_pull.py` | Cameras at the same centre use different codecs (Camera 6 was H.264, Camera 29 was HEVC), and all NVR audio is G.711 µ-law (telephony codec). Gemini won't accept the mix. Always transcoding is simpler than codec-conditional logic and only adds ~10 min per 80-min segment. |
| 2026-06-02 | **`cctv_cameras.xlsx` kept to 6 columns**; activity / teacher / day-of-week / classroom come from Openhouse's daily-schedule database at analysis time | Schedule data is already maintained by Openhouse's teacher app — duplicating it in TQM creates a sync problem. The CCTV config is a stable lookup (camera → centre → NVR → default subject); the time-varying activity context joins later. |

---

## Architecture summary

```
score_cli.py                      ← entry point for Phase 0a
   │
   ▼
SessionMeta + local video path
   │
   ▼
vision_observe()                  ← Gemini 2.5 Flash, single call per video
   │   (uploads to Gemini Files API; emits transcript + visual observations)
   ▼
Transcript + VisualObservations
   │
   ▼
score_session()                   ← Claude Sonnet 4.6, parallel fan-out
   │   (asyncio.gather over per-dimension prompts)
   ▼
SessionScores (per-dimension scores with evidence)
   │
   ▼
data/sessions/<id>/scores_<rubric>_<hash>.json
```

Future phases bolt onto this without changing it:
- Phase 0b: a FastAPI app that triggers `score_cli.py`'s functions via background tasks, plus an HTML UI reading the same JSON files.
- Phase 1+: the same pipeline functions, wrapped as workflow steps in Inngest/Temporal.
- Phase 2: the coaching agent reads these JSONs as its data layer.

---

## Open questions

- **Few-shot exemplars** in the scoring prompt — skipped in v0.1; populate after first calibration round using sessions where model and humans agreed at high confidence
- **`anchor_matched` representation** — currently verbatim from rubric; may switch to anchor IDs if model paraphrases too often
- **Stress-test** — verify scoring discriminates downward (low scores on weak transcripts), not yet done
- **Negative Climate consistency** — `scoring_direction: reverse` field in YAML is now misleading after we standardized 5=good; remove on next rubric pass
- **Vision pass quality** — TBD whether Gemini's transcript is good enough or we need AssemblyAI/Deepgram for diarization
- **Prompt versioning UI** — for now, prompts are git-versioned; later we may need a runtime registry
- **Activity-type mismatch** — see "Known limitations". Tier 1 prompt rule pending; Tier 2 activity classification + per-dimension applicability pending.

## Reference repo (`nikhil2197/agentic_vid_QA`) — defects observed by running

These were either predicted in the original audit or surfaced when we ran the working copy at `/Users/oh/Desktop/agentic_vid_QA_run` against a real video on 2026-05-18. Recorded so we don't repeat them in our own pipeline.

- **Cache-by-date in `transcript_builder.py`** (predicted, then confirmed in production). The node grabs the most recent `transcript_*.txt` from `data/transcripts/` regardless of which video is in the catalog. We loaded `transcript_2025-08-12.txt` (from the colleague's August demo, mentioning a child named "Maya") while analysing a completely different 35-min video — the pipeline produced a confident, fabricated parent update about Maya. Our fix in our own pipeline: cache by `session_id` + content hash, never by date.
- **`question_refiner` template doesn't handle missing child info.** When `child_identifier` legitimately decides "child identification not required" (e.g. for a generic question about the whole class), `child_id_info` is empty, and the refiner's prompt template breaks — the model responds with its own meta-prompt ("Please provide the Original Question and the Child ID Info...") and that meta-prompt then flows downstream as the "refined question". Every later node, including `video_analyzers`, gets asked to act on nonsense.
- **`followup_advisor` is video-blind.** Follow-up questions never re-trigger `video_analyzers` even when they clearly require fresh video evidence (e.g. "Did the girl in the black t-shirt engage today?"). The advisor only sees conversation history, so it produces generic empathetic filler when grounded answers are needed.
- **`video_picker` crashes on string time fields.** Their code does `end-time - start-time` arithmetic assuming both are floats (YAML 1.1 sexagesimal). If a catalog uses quoted `"HH:MM"` strings (more readable), the subtraction raises `TypeError: unsupported operand type(s) for -: 'float' and 'str'`. The flow continues because picker failure defaults to all videos, but the bug is real.
- **`call_video` re-uploads on every call** (their original Vertex AI adapter). Fine in single-shot single-video flows, painful for chunked / multi-call. Our `llm_adapter_local.py` patch added an in-memory upload cache.
- **Lite models are insufficient for chunked Q&A.** `gemini-3.1-flash-lite` produced 133–323 char per-chunk outputs (vs the ~2 KB needed for substantive observations). Their `composer` then hallucinated warm filler over the empty signal. Stick to full-flash or pro for any chunked analysis.
- **Per-chunk attribution patch** (added 2026-05-18). Their `video_analyzers` originally kept per-chunk outputs only in memory, then handed them to `composer` and dropped them. We patched it to persist each chunk to `data/chunks/<timestamp>_<vidid>/chunk_NN_minXX-YY.txt` plus a `_question.txt` for context. Critical for debugging: without this, you cannot trace which composer claim came from which chunk vs which was interpolated.
- **Grounded core + interpolated periphery (the Maya case).** On the same 35-min video, the colleague's pipeline asserted *"Maya, wearing a pink dress, enters the classroom at 09:39:50 and is greeted by her teachers"*. Verifying: (a) Maya's name confirmed real — teacher audibly greets "Maya, good morning Maya" in the audio; (b) chunk 1 contains the sentence — so composer didn't fabricate it from nothing; (c) but "pink dress / 09:39:50 / exploration narrative" are unverified — model interpolated around the real audio anchor. **This is the cleanest concrete illustration we have of why our scoring prompt requires verbatim transcript quotes and timestamped evidence per dimension. The colleague's pipeline produces compelling but unauditable narratives; ours is built so every claim is traceable.**

## Cost and throughput (measured 2026-05-18, paid tier 1)

Inference rate card used: Gemini 2.5 Flash $0.30/$2.50 per 1M tokens in/out; Claude Sonnet 4.6 $3/$15 per 1M in/out (or $0.30 for cached input reads). Video tokenization ~263 tok/sec.

**Per single run on a 35-min Pre-K video:**

| | Theirs (1 parent question) | Ours (1 session, no cache) | Ours (1 session, w/ prompt cache) |
|---|---|---|---|
| API calls | 12 Gemini | 7 Gemini + 10 Claude | 7 Gemini + 10 Claude (cached) |
| Wall-clock | ~10–15 min | ~10–13 min | ~10–13 min |
| Cost | **$0.36** | **$2.33** | **$0.83** |
| Output | 1 narrative paragraph | 10 dimension scores w/ evidence | same |

**Per classroom per day (20 children, 1 session/day):**

| Pipeline | Daily work | Daily inference cost |
|---|---|---|
| Theirs: 20 parents × 1 question each | 20 pipeline runs | ~$7.20 |
| Ours (no cache) | 1 score + per-parent digests (cheap) | ~$2.43 |
| Ours (cached) | same | ~$0.93 |

**Per school per year (30 classrooms, ~6 K sessions):**

| Item | Cost/year |
|---|---|
| Ours, with prompt caching | ~$5,500 |
| Video storage (GCS standard → coldline at day 30, ~9 TB at year-end) | ~$1,500 |
| Structured artifact storage | <$10 |
| **Total infrastructure per school per year** | **~$7,000** |

**Key amortization insight:** the colleague's pipeline pays *per consumer*; ours pays *per session*. At 20 parents per classroom, ours is ~3–8× cheaper per classroom-day. At scale this is the dominant economic argument for our architecture.

**Optimizations queued for Phase 1:**
- Prompt caching on the per-dimension scoring calls (~60% cost reduction, single config change)
- Re-score-from-cached-vision when rubric changes (~40% cost reduction on re-scores)

### Long-video pipeline (`score_long_video.py`) measurements — 2026-05-20

Pipeline = boundary detection (1 Gemini call on whole video) + ffmpeg extract before/after + vision + items + score against both rubrics for each segment.

**Per long video (no caching, paid tier):**

| Stage | Cost | Time |
|---|---|---|
| Boundary detection (Gemini, whole video) | ~$0.10 | ~2–4 min |
| ffmpeg extract before + after | — | <10 s |
| Per segment × 2: vision + items + 10 score calls (5 dims × 2 rubrics) | ~$0.73 each | ~3–5 min each |
| **Total** | **~$1.56** | **~10–15 min** |

**Scale target: 100 long videos per day.** Sequential = ~20 h (too slow). See "Path to 100/day" below.

### Path to 100 long videos per day — optimization stack

Sequential 100 × 12 min = ~20 h. Target: under 4 h (ideally overnight). Levers ordered by impact:

1. **Parallelism across videos (biggest win, easiest).** Run N video pipelines concurrently with an asyncio semaphore. At paid Gemini tier 1 (~1M input tokens/min), the practical cap is around 8–10 concurrent workers before token throughput becomes the bottleneck. **At 10 concurrent: 100 × 12 / 10 ≈ 120 min ≈ 2 h.** Net: ~10× wall-clock reduction with one file of code.

2. **Parallelise before/after segments within each video.** Boundary detection blocks; but once boundaries are known, the two segments are independent. Currently sequential; `asyncio.gather` would let them run together. **Saves ~3–4 min per video (~30% per-video).** Combined with #1: 100 videos in ~75 min.

3. **Claude prompt caching on the per-dim scoring fan-out.** Each rubric scores 5 dims, each call sends the same observations + transcript + system prompt. Caching cuts that scoring cost from ~$1.30 → ~$0.50 per video (~50% of total cost). Marginal time win (a few seconds per scoring run), but cost matters at 100/day: ~$150 → ~$80/day. **Single config change.**

4. **Single upload + `video_metadata` offsets.** Today we upload the full video for boundary detection, then re-upload each 5-min clip. Reusing the original upload with offsets for the segment vision passes saves ~2 min + ~$0.05 per video. ~1 h to implement.

5. **Local CV for boundary detection.** Replaces the ~2–4 min Gemini call with a ~30 s YOLO frame scan. Saves ~$0.10 + ~3 min per video. **Net: ~8 min per video** with the rest of the stack. ~3 h to implement (model weights, scale to children-vs-adults distinction).

6. **Workflow engine (Inngest / Temporal) for Phase 2.** Durable retries, per-video isolation, replay. Doesn't reduce wall-clock per video; reduces ops pain at scale (retries, monitoring, partial failures). Worth introducing when daily volume crosses ~50 and the homemade asyncio orchestrator starts feeling fragile.

**Realistic stack to hit the 100/day target in Phase 1:** #1 + #2 + #3. Implementation cost: ~2–3 hours of code. Outcome: ~$80/day, ~1–1.5 h wall-clock for 100 videos. #4–6 are good further-out optimizations but not required to hit the headline target.

## Calibration findings — first batch (2026-05-19 / 20)

First real calibration of the playground (v0.2.3) and toy_design (v0.2.0) rubrics against team scores. 7 videos × 2 rubrics × model + 3 human raters (Akshay = strict; Pari = moderate; Ayesha = generous + most detailed comments). Team's source-of-truth sheet: `~/Downloads/Setting Review - Sheet1.pdf`.

**Open mapping question (Video 2):** model's vision pass on `multisensorybins_1505.MOV` described a sand-and-blocks setup, but the team's notes for Video 2 describe a water/fishing setup (magnetic rods, fish, tubs of water). Either the file the team scored differs from what Gemini analyzed, or Gemini misidentified the contents. **Resolve before drawing further conclusions on Video 2.**

### Major disagreement patterns to address

Listed in priority order — each is a candidate for rubric tightening or pipeline change, but all are **deferred for later** while we discuss higher-level structure.

1. **Clean up: model is far too conservative.** Model returns `ie` on all 7 videos; team often scores 1 with comments like *"Auto cleaned — nothing extra required"*, *"not needed unless thrown by the kid"*, *"in such engagements kids do not have to clean."* The team treats *"the activity is self-cleaning by design"* as a positive design property worth a 1. Our rubric reserves `ie` for unobservable cleanup, which over-counts. **Fix:** add an explicit rule — *if the activity is self-cleaning by nature OR cleanup is not part of the activity design, score 1.* `ie` should be reserved for cases where storage IS needed but truly invisible.

2. **Multi-sensory: model too generous.** Model gives 1 on Damru, Sand play, and Nature sensory bin (HRBR) where team averages 0.17 / 0.33 / 0.25 respectively. The team is reading the dim as *"is the multi-sensory engagement deliberate and rich?"*; model is reading it as *"are 2+ senses incidentally present?"*. **Fix:** require deliberate multi-sensory design intent (visible sound elements, deliberate texture variety, etc.) for a 1, not just incidental presence.

3. **Challenge Adjustment: model too strict.** Model = 0 across all 7 videos. Team gives 0.5–1 when material variety supports natural self-pacing (*"different tools were available"*, *"rolling stamps, paints on plates"*). **Fix:** allow material variety + open-ended use to earn 0.5; reserve 0 for truly single-fixed-material activities.

4. **Spark Curiosity / Anchor & Choice on nature bins (data-capture issue, not rubric).** Team gave Nature sensory bin (1) Spark = 1.0 and Anchor & Choice = 1.0; model gave 0.5 / 0.5. The team sees the richness (animals, leaves, mud, pebbles arranged thoughtfully); the model's vision pass aggregates this as "sensory bin" without enumerating the variety. Root cause: same Gemini enumeration limitation we hit on floor painting and multisensory bins. **Fix:** stronger activity_context with explicit materials list, OR stricter enumeration prompt, OR move to a paid-tier vision model with better detail capture.

5. **Movement on Floor painting (reverse direction — team stricter).** Team avg 0.17, model 1. Team penalized because movement flow was unclear ("some kids walked out through the other door"). Model accepted general access + clear posture. This is rubric-spirit vs rubric-letter — worth a conversation rather than an immediate change.

### Per-rater behavior

- **Akshay:** Strict. Frequently gives 0 on Cleanup, Challenge Adjustment, Narrative Setting when not explicitly seen. Skipped scoring on Videos 6 and 7 entirely.
- **Pari:** Moderate. Often agrees with Akshay on the low end and Ayesha on the high end. Most reliable middle-ground baseline.
- **Ayesha:** Most generous and most detailed comments. Tends to give credit for design intent and inferred context; the model agrees with her least on lower-end dims.

Best correlation with model: Pari (closest moderate rater). Best to use Pari's scores as the calibration anchor when picking a single human reference.

### Architecture notes worth preserving

- The disagreement on Cleanup is the strongest single signal that the rubric needs *both* `not_applicable` and `insufficient_evidence` as distinct outcomes (we earlier filed `not_applicable` as a Phase 2 idea; this calibration data argues for moving it forward).
- Multi-sensory + Challenge Adjustment disagreement suggests our rubric anchors are too literal vs the team's intent-based reading. Worth a structural conversation about *intent vs evidence* scoring (the same question that came up with the original session_003 Marathi song video).

## Boundary detection for long videos (built; iterated extensively 2026-05-22)

When the input is a longer recording (e.g. 30-min class) rather than a pre-clipped 5-min segment, we want to score the SPACE in its prepared state (before children arrive) and its post-class state (after they leave). The two scoring windows are bounded by **person presence**, not by simple "first 5 min" / "last 5 min" of the recording.

### Approach chosen for v1 (built later, not now): single Gemini call

One Gemini call per video that returns the first-child-visible and last-child-visible timestamps. Then ffmpeg extracts the 5-min windows around those boundaries. Then the existing pipeline scores each extracted segment against both rubrics. Side-by-side before/after report shows design quality (before) and design resilience (after).

- **Cost:** ~$0.10 per video (one detection call + the existing scoring pipeline on two 5-min clips)
- **Time:** ~10-15 min wall clock per long video
- **Accuracy:** ±10-30 sec on boundary timestamps — fine for 5-min windows
- **Prompt distinguishes** "first child" from "first person" — adults setting up early are still part of the "before" state we want to score

### Alternative considered: local computer-vision detection

Run a pre-trained person detector (YOLOv8n, MediaPipe, etc.) on sampled frames locally. No API calls.

- **Cost:** $0 per video (just CPU)
- **Time:** ~30 sec per video
- **New deps:** ~150 MB of model weights, `ultralytics` package
- **Limitation:** off-the-shelf detectors mark adults and children equally as `person`. Would need a second pass (small classifier OR a Gemini call on the boundary frames) to verify the boundaries are child-bounded, not adult-bounded.

**Why we're not using local CV yet:** at current scale (handful of videos for prototyping), Gemini's ~$0.10/video is negligible and avoids a new dependency. **Switch to local CV when:** processing dozens of videos per day (Phase 2/3), or when API cost / latency becomes meaningful. The natural maturity path is:
1. **Now**: Gemini single call (cheap, simple, works)
2. **Phase 1+ at scale**: Local YOLO for person presence + a small Gemini call on the boundary frames only to verify "is this a child" — cuts cost ~95% while preserving accuracy

### Negative finding from `nikhil2197/Classroom-Video-Chunk-And-Analyze`

Worth noting: the colleague's other repo (`Classroom-Video-Chunk-And-Analyze`) explicitly tried **GCP Video Intelligence API's built-in `PERSON_DETECTION`** for a related problem and found it produced "limited or no actionable insights" on classroom video. This rules out a tempting "Approach E" (managed cloud CV service) for our use case — at least for the typical preschool video quality we're dealing with.

### One small reusable bit from that repo

Their `split_video()` ffmpeg helper (in `GCPVideoAI_GPT4o_Pipe/chunk_and_annotate.py`) is a ~10-line wrapper around `ffmpeg ... -f segment -segment_time` that we can reuse as-is when we build the before/after extraction. Saves a few minutes.

### Implementation journey + wall-clock leak fix (2026-05-22)

The "single Gemini call returns first/last timestamps" design above turned out to be **harder than expected** because of a systemic failure mode we hadn't anticipated: the model can't reliably ignore the burned-in CCTV wall-clock overlay. What we ended up with after 7 prompt iterations and 5 pipeline-side safety nets is documented here for future reference.

#### The dominant failure mode: wall-clock leak

CCTV recordings have a large burned-in clock overlay (date + time, e.g. `09-09-2025 Tue 09:40:43` at the top of the frame). When asked for elapsed-time timestamps, the model:

1. **Reads the clock correctly** (visual capability is fine)
2. **Tries to "convert" it to elapsed time using a broken heuristic** — usually "strip the hours digit"
3. Reports e.g. `last_child_visible_at: "00:42:00"` when the actual wall-clock at that moment was `09:42:00`

This produced consistent garbage outputs across multiple prompt revisions (`v0.2.0` through `v0.6.0`). The model is NOT semantically confused about "wall-clock vs elapsed" — it can explain the difference correctly if asked directly. The failure is in the *conversion math*, plus *visual saliency* (the clock is large and prominent; textual instructions to ignore it lose to that).

#### Key insight that unlocked the fix

**The model is RELIABLE at READING the clock; UNRELIABLE at converting wall-clock to elapsed.** So instead of fighting the model's tendency to read the clock, we now ASK it to read the clock and report verbatim. The pipeline does the subtraction in Python.

This is the v0.7.0 architectural change:

| Concern | v0.6.0 and earlier | v0.7.0+ |
|---|---|---|
| Vision (read clock) | Reliable | Reliable — same |
| Conversion | Done by model with broken heuristic | Done by pipeline with `datetime` subtraction |
| Accuracy floor | Off by minutes when leak hits | Off by ≤1 sec (clock resolution) |

#### The v0.7.x prompt design

Model returns three wall-clock readings:
- `video_start_wall_clock` — clock at first frame
- `first_child_wall_clock` — clock when first child appears and stays
- `last_child_wall_clock` — clock when last child finally leaves

Plus the existing grounded `first_child_evidence` and `last_child_evidence`. Pipeline subtracts in `pipeline.boundaries._subtract_clocks` and populates `first_child_visible_at` / `last_child_visible_at` from the math.

If no clock is visible (no CCTV overlay), all wall-clock fields are null and the model fills the elapsed fields directly as before.

**v0.7.1 added REVERSE SCAN**: tells the model to find `last_child` by starting from the END of the video and working backwards, with explicit end-verification ("after your claimed last_child, no child should appear in any subsequent frame"). This catches the *intermediate-departure* error where the model anchors to a mid-class departure instead of the actual final exit.

#### The five pipeline-side safety nets

Located in `score_long_video.compute_windows`. Catch model misbehaviour that prompts can't reliably prevent:

1. **`_validate_timestamp` 5% grace** — rejects timestamps beyond the video duration (catches surviving wall-clock leaks that escape pipeline subtraction).
2. **Fix 1: `self_check_passed=False`** — model flagged its own answer as unreliable → fallback to first 5 / last 5.
3. **Fix 2: both timestamps are `00:00:00`** — model emitted defaults instead of null/null (Colouring v0.4.0 case) → fallback.
4. **Fix 3: order violation `last < first`** — Balloon dance `first=09:00, last=00:00` case → treat last as null.
5. **Fix 4: "first invalidated + last≈0"** — Colouring v0.5.0 case where wall-clock leak invalidates first but last sneaks through as `00:00:00`. Treat both as null.
6. **Fix 5: implied class duration too short** — `last_s − first_s < max(before_post, after_pre)` → before/after windows would overlap → not a usable boundary, fall back.

**Edge alignment** in `_before_window` / `_after_window`: when `first_child` is at/near video start, before-window is `(0, before_total)` (clean first 5 min) rather than asymmetric (which would clamp into a 4-min window). Same for `last_child` near video end.

#### Other settings that matter

- **`temperature=0` for `call_gemini_video`** (was 0.3). Critical for reproducibility — earlier we saw Balloon Dance return `00:09:00 / 00:10:00` one day and `00:09:00 / 00:31:59` another with identical inputs. Same-input runs now produce stable outputs (at least for the model's *failure* mode; success cases were already stable).
- **`upload_video` has `@retry_external(max_attempts=4)`** — Files API gateway returns 503 mid-upload under load; retries with fresh resumable session.

#### Measured results vs ground truth (2026-05-22, v0.7.1, 3 of 4 videos tested)

| Video | first_child elapsed | last_child elapsed | Ground truth | Verdict |
|---|---|---|---|---|
| Colouring (21:42) | `00:03:03` | `00:19:49` | first `~00:03:04`, last `~00:21:00` | ✅ first within 1 sec; last ~1 min short |
| Morning circle time (35:17) | `00:00:00` | `00:30:08` | first `~00:00:17`, last `~00:35:17` | 🟡 last ~5 min short; REVERSE SCAN closed ~10 min of the original gap but couldn't get the full way |
| circle_time (22:38) | `00:00:00` | `00:24:54` → invalidated → fallback to last 5 min | first ≈ start, last clock `10:13:59` confirmed correct | 🟡 Model misread start clock by ~2 min; safety net catches → after-window correctly captures end of class |
| Balloon dance (32:00) | _untested today (daily RPD quota)_ | | | — |

#### What remains imperfect

- Model can pick an *intermediate departure* and call it the last (Morning circle time `last_child=10:09:51` instead of `10:15:00` ground truth). REVERSE SCAN narrows the gap but doesn't close it.
- Model can misread one of the three clocks (circle_time `start_wall_clock` was 2 min too early). Pipeline catches the resulting out-of-range elapsed time and falls back.
- Self-check (`self_check_passed`) is not reliable — the model marks `True` even on obvious violations (e.g. `last_child > video_duration`). It's still useful when it returns `False` (rare but informative); ignored when `True`.

These remaining errors are *bounded* — within a few minutes — and the pipeline's 5-min before/after windows give some forgiveness. The pre-v0.7.0 system could be wrong by *hours* (wall-clock leak) or produce *0-sec windows* (`last=00:00:00` collapsed). We're now in the territory of "boundaries are approximate but not catastrophic."

#### What was explicitly rejected

- **Telling the model "ignore the burned-in clock"** in various forms across v0.2.0–v0.6.0. Doesn't work. Visual saliency wins over instructions.
- **Shape A/B/C taxonomy in v0.4.0**. Biased the model into over-applying "Shape B = null/null" even for clear setup→class→packup videos. Reverted in v0.5.0.
- **Cropping/masking the overlay with ffmpeg before upload** (discussed 2026-05-22). Would be bulletproof but requires per-camera config (clock position varies). User chose the wall-clock subtraction approach instead as more general.
- **Two-call boundary detection** (one for start, one for end). Would double cost. The single-call + pipeline subtraction is good enough.

#### Path to better accuracy (deferred)

If a video has misread clocks or the model picks intermediate departures often enough to matter:

1. **Duration-anchored start fallback**: when `(last_wc − start_wc) > duration`, recompute `start = last_wc − ffprobe_duration`. Trusts the more-anchored end + the known duration. ~15 min of code.
2. **Local CV (YOLOv8) for person presence** + a small Gemini call on boundary frames to verify "is this a child". This was the original Phase 2/3 plan; nothing about today's experience changes that.
3. **Two-call boundary detection** focused separately on first vs last with the relevant chunk of video only.

All deferred. Current accuracy is sufficient for the 5-min before/after window architecture.

## Audio + Video patching workflow (designed, deferred)

**Use case:** in our setup we'll commonly have *two separate source recordings* per session — a CCTV video file (good visual coverage, distant/noisy audio) and a separate clean audio file (lapel mic on teacher, table mic, room mic). We want to combine them into a single video file with the clean audio replacing the CCTV's degraded audio. The combined file then flows into the existing scoring pipeline unchanged.

**Why this is architecturally better than the colleague's approach.** Their repo (`nikhil2197/Classroom-Video-Chunk-And-Analyze`) had only one source — degraded CCTV audio — and the entire history of that repo is a sequence of workarounds to *recover* signal from a noisy single source: Whisper v2 → Demucs voice isolation → Whisper-large-v3 → ElevenLabs. Every step was fighting bad input. With a separate clean mic, we skip all of that — the combined file goes straight through Gemini with no source-separation gymnastics required.

### Pipeline placement

Pre-processing step, fully separate from scoring:

```
[CCTV video]   [Lapel mic audio]
      │              │
      ▼              ▼
   ┌─────────────────────┐
   │  combine_av.py      │   ← new tool to build
   │  (align + mux)      │
   └──────────┬──────────┘
              ▼
        [combined.mp4]
              │
              ▼
   Existing pipeline (no changes):
   - score_cli.py
   - score_long_video.py
   - batch_score.py
```

The combined file is just a normal video to everything downstream — no changes needed in `vision.py`, `score.py`, etc.

### Three implementation tiers

**V1 — Manual offset (start here when ready).** User passes `--audio-offset-seconds <N>` from the CLI; ffmpeg does the mux. ~30 lines of code:

```bash
ffmpeg -i video.mp4 -itsoffset <N> -i audio.wav \
  -map 0:v -map 1:a -c:v copy -c:a aac output.mp4
```

Works immediately if the user can read offsets from a slate, clapperboard, or by eye. Sufficient for prototyping.

**V2 — Clap / sync-sound auto-alignment.** User claps at start of each session; we detect the clap peak in both audio waveforms and compute the offset automatically. ~50 lines on top of V1. Needs `scipy` or `librosa`. Operationally requires a "everyone claps at start" process the team adopts.

**V3 — Cross-correlation auto-alignment (production).** Compare the audio waveforms of both sources (CCTV has *some* audio even if degraded). Find the offset that maximizes cross-correlation. This is what tools like PluralEyes do. ~80 lines, handles drift if applied in chunks (every ~5 min for long recordings). The "right" long-term answer; deferred until V1/V2 prove insufficient.

### Drift

Over 30+ min recordings, separate devices' clocks drift relative to each other (typically ±0.1%, so ±1.8 sec at 30 min). Visible as lip-sync slip by the end. **Not relevant for our scoring use case** (rubric-level analysis is insensitive to ±1 sec audio offset). Becomes relevant only if we ever build a teacher-facing playback UI that needs precise lip sync.

### What we'd need to build V1

1. A sample pair of files (one CCTV video + one separate audio recording of the same session) to verify the mux works on real Openhouse data.
2. Confirmation of the typical audio source (lapel mic? iPad? handheld recorder?) — affects format/channel-layout handling.
3. Confirmation of whether the team already has a sync method in use (clapperboard, audible cue, timestamp metadata) — if yes, V2 may be easier to start with than V1.

### Deferred for now

No code written. Workflow design captured here so we can build cleanly when the team starts producing dual-source recordings. Estimated build time: ~30 min for V1, ~1.5 hr for V2 on top of V1, ~3 hr for V3.

---

## CCTV access pipeline (built 2026-06-01 / 02)

Replaces the manual "walk to centre → USB into NVR → carry home" workflow with
programmatic, time-windowed pulls into `data/raw/` from anywhere on the
internet.

### Architecture

```
Mac (anywhere)              Tailnet                Centre LAN (192.168.0.0/24)
─────────────────           ─────────              ────────────────────────────
cctv_pull.py        →       Tailscale       →     Windows laptop
(reads xlsx)                relay                  (advertises subnet)
                                                            ↓
                                                   Hikvision NVR
                                                   DS-7632NXI-K2
                                                   @ 192.168.0.104
                                                            ↓
                                                   ISAPI: search + download
                                                            ↓
                                                   ffmpeg transcode → data/raw/
```

Three components in the path:
1. **Tailscale subnet router** — a Windows laptop physically at the centre advertises `192.168.0.0/24`, letting any Tailscale-connected Mac reach NVR + cameras over the internet
2. **ISAPI** — Hikvision's documented HTTP API on port 80; `/ContentMgmt/search` + `/ContentMgmt/download` give us programmatic access without the Mac iVMS-4200 dance or the Windows-only web-UI plugin
3. **Local transcoder** — ffmpeg normalises mixed NVR output (H.264 / HEVC + µ-law) to H.264/AAC for Gemini

### What we tried before settling on Tailscale + ISAPI

| Rejected | Why |
|---|---|
| iVMS-4200 Mac client | NVR registered as Cloud P2P → download button disabled regardless of credentials. iVMS-4200 in IP/Domain mode also failed to authenticate over the SDK port despite ISAPI auth succeeding. |
| NVR web UI download | Requires Windows-only ActiveX/NPAPI plugin; greyed out on Mac browsers |
| Hik-Connect mobile app | Limited clip length, lossy re-encode, manual every time |
| Port-forwarding NVR to public internet | NVR admin interface exposed; Hikvision NVRs are heavily targeted by botnets |
| Phone as Tailscale subnet router | iOS and Android don't support subnet routing — phones can only be Tailscale clients |
| Mac App Store Tailscale | Sandboxed: CLI crashes with `BundleIdentifiers.swift` error, "Use Tailscale subnets" toggle hidden. Use standalone `.pkg` from `pkgs.tailscale.com/stable/Tailscale-latest.pkg` instead. |

### Schema — `data/cctv_cameras.xlsx`

Lean 6-column config. Activity, teacher, day-of-week, classroom etc. are
intentionally NOT here — those come from Openhouse's existing daily-schedule
database (joined at analysis time, not at ingestion).

| column | example | purpose |
|---|---|---|
| `camera_id` | `D14` | Camera label on NVR |
| `centre_name` | `HRBR` | Centre code (becomes FK to centres table later) |
| `nvr_host` | `192.168.0.104` | Per-centre NVR IP (multi-centre ready) |
| `subject` | `public_speaking` | What this camera films; tagged on output filename |
| `is_active` | `true` | Soft enable/disable |
| `notes` | (free text) | |

Initial 3 cameras seeded: D14 public_speaking, D28 art, D29 robotics — all at HRBR.

### Track ID convention (Hikvision)

`trackID = (channel × 100) + 1` for main stream. So:
- D14 → trackID 1401
- D29 → trackID 2901
- D32 → trackID 3201

Confirmed against `/ISAPI/ContentMgmt/record/tracks` — all 32 channels provisioned. Note: tracks report `<Enable>false</Enable>` but recordings ARE present, so that field describes track-config state, not live recording state.

### Time zone quirk

The NVR labels timestamps as `Z` (UTC) but they're actually NVR-local IST.
- In search requests: use `+05:30` offset (works)
- In responses: strip the `Z`, treat as naive IST
- `cctv_pull.py` does this via `parse_hik_time` so the rest of the code can ignore it

### Codec normalization

Cameras at the same centre use different codecs:
- Camera 6 → H.264 Baseline Profile
- Camera 29 → HEVC (H.265)
- All audio → G.711 µ-law (8 kHz mono — telephony codec)

`cctv_pull.py` probes each downloaded segment with ffprobe and:
- If video is `h264` AND audio is `aac` (or no audio) → just rename
- Otherwise → transcode (libx264 CRF 23 fast + AAC 96 kbps), `+faststart` for streaming

### What's needed at each centre (one-time setup)

The Windows laptop hosting Tailscale needs three things beyond "Tailscale installed and connected":

1. `tailscale up --advertise-routes=192.168.0.0/24` from **admin** PowerShell
2. Route approved in Tailscale admin UI (`login.tailscale.com/admin/machines`)
3. Windows IP forwarding registry: `IPEnableRouter=1` under `HKLM\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters` + reboot

Without **#3** specifically, Tailscale shows Connected and the route is approved — but packets don't actually flow through the Windows machine. Lost ~2 hours to this; documented so the next centre's setup doesn't.

### Open issues on this pipeline

1. **Subnet collision on user's home WiFi.** Home WiFi uses `192.168.0.0/24` (same as centre). macOS routes `192.168.0.104` via the local interface, never via Tailscale. Workaround in use: phone hotspot. Permanent fix: change home router to `10.10.10.0/24` or similar.
2. **Continuous recording not verified per channel.** Camera 5 only had 4 segments in 25 hours — motion-triggered, not continuous. Need to flip each classroom channel to **Continuous** at `Menu → Configuration → Record → Schedule` on the NVR before relying on the pipeline for production.
3. **NVR admin password was pasted in chat** during debugging. Rotate + update `.env` before scheduled pulls go live.
4. **Windows side not yet fully applied** at HRBR. User to apply IP-forwarding registry tweak + reboot on next centre visit; first end-to-end remote pull deferred until then.

---

## Known limitations

- **Audio nuance lost in text intermediate.** Gemini's transcript captures *words* but not *vocal qualities* — tone, warmth, pacing, energy, pauses. Several dimensions (Positive Climate verbal warmth, Teacher Sensitivity, Quality of Feedback affect) suffer. Possible fixes: (a) extend vision prompt to also describe audio characteristics, (b) two-pass with a separate audio-feature analyzer, (c) dedicated prosody/diarization service.
- **Camera angle dependency.** Wide CCTV with children's backs to the camera and teacher off-screen invalidates most visual-dependent scoring. No code fix; operational guidance for camera placement is needed.
- **Activity-type vs rubric fit.** A 1-min single-activity clip (e.g. a song) cannot fairly score Quality of Feedback or Concept Development — those dimensions need dialogue activities. CLASS was designed for 15–20 min cycles spanning multiple activity types. Two fixes pending: (1) prompt rule preferring `insufficient_evidence` over a low score when activity inherently doesn't surface a dimension; (2) activity classification in vision pass + per-dimension `applicable_to` rules in rubric.
- **Transcript completeness on long videos.** SOLVED 2026-05-18 by chunked vision processing. (Was: 65 K output token cap truncated transcripts on long chatty sessions; observations preserved but transcript-driven dimensions degraded.)
- **Audio is G.711 µ-law at 8 kHz mono** at NVR source. Telephony quality — speech is intelligible but vocal nuance (pitch, energy, prosody) is lost before it reaches Gemini. Combined with the existing "audio nuance lost in text intermediate" limitation, this is a double hit for Positive Climate / Teacher Sensitivity / Quality of Feedback scoring. Real fix: separate mic capture at source (planned in the Audio+Video patching workflow above).
- **NVR recording schedule not verified per channel.** Camera 5 had only 4 segments in a 25-hour search window (motion-triggered, not continuous). Other channels likely the same. Need to switch each classroom camera to **Continuous** recording at the NVR's UI before relying on scheduled pulls.

---

## Next steps

1. **Test the full pipeline on one real classroom video** ← we are here
2. Hand-score 3–5 of those videos by hand using the rubric
3. Build the calibration script (per-dimension agreement report)
4. Recruit one experienced coach as a second rater on a subset
5. Iterate prompts / rubric based on calibration findings — log iterations in this doc
6. Decide Phase 0b kickoff based on Phase 0a outcome (target: rubric stable enough that pilot teachers will trust scores)

### CCTV pipeline workstream

7. **Apply Windows-side fix at HRBR** — `tailscale up --advertise-routes=192.168.0.0/24` + `IPEnableRouter=1` registry + reboot (on next centre visit)
8. **Rotate NVR admin password** and update `.env` (was shared in chat during debugging)
9. **First end-to-end remote pull**: after #7, run `python scripts/cctv_pull.py --date <yesterday> --camera D29 --dry-run` from home, confirm segments, then drop `--dry-run`
10. **Verify continuous recording** is enabled on D14 / D28 / D29 at the NVR UI (`Menu → Configuration → Record → Schedule`) before scheduling
11. **Migrate `cctv_cameras.xlsx` → SQLite table** in `tqm.db` once the 6-column schema settles
12. **Join with Openhouse daily-schedule DB** for per-session activity / teacher / age context at analysis time
13. **Daily cron** for scheduled pulls once a week of remote pulls runs cleanly
14. **Permanent fix for subnet collision**: change home router LAN from `192.168.0.0/24` to `10.10.10.0/24` (10-min disruption, eliminates the hotspot workaround)

---

## Changelog

- **2026-06-02**: Built `scripts/cctv_pull.py` + `data/cctv_cameras.xlsx`. 6-column lean schema (camera_id, centre_name, nvr_host, subject, is_active, notes) — activity/teacher/schedule deliberately deferred to Openhouse's daily-schedule DB. Seeded 3 cameras at HRBR (D14 public_speaking, D28 art, D29 robotics). Pipeline does ISAPI search → segment download → ffprobe codec detection → auto-transcode to H.264/AAC if needed → idempotent rename into `data/raw/`. Supports `--dry-run`, `--no-transcode`, single-camera / `--all` modes. Output naming: `{camera_id}_{centre}_{subject}_{YYYYMMDD}_{startHHMMSS}.mp4`. Full end-to-end remote download deferred until Windows IP-forwarding fix is applied at HRBR.
- **2026-06-01**: CCTV access discovery + Tailscale architecture. Mapped the full constraint chain (NVR registered as Cloud P2P → iVMS-4200 download disabled → web UI download needs Windows ActiveX → phone can't be a subnet router → Mac App Store Tailscale is sandboxed and missing the subnet toggle). Confirmed working stack: standalone Tailscale on Mac + Windows laptop at centre as subnet router + ISAPI from `curl`/Python. Verified `/System/deviceInfo`, `/ContentMgmt/record/tracks`, `/ContentMgmt/search`, `/ContentMgmt/download` all functional over digest auth. Identified NVR as DS-7632NXI-K2, firmware V4.83.011. nmap inventory: 32 Hikvision IP cameras on 192.168.0.2–33, NVR at 192.168.0.104. Codec finding: mixed H.264 / HEVC across cameras + G.711 µ-law audio throughout — motivates the always-transcode decision. Subnet collision flagged (home WiFi also 192.168.0.0/24); current workaround is phone hotspot. Test LAN download of Camera 6 = 1.06 GB clean H.264 BP; remote search confirmed working for Camera 29; full remote download blocked pending Windows-side IP-forwarding registry tweak.
- **2026-05-22**: Boundary detection iterated extensively after a systemic wall-clock leak surfaced in real CCTV recordings. Final architecture (prompt v0.7.1 + pipeline subtraction + 5 safety nets + `temperature=0`) detailed in "Boundary detection for long videos" section above. Headline outcomes: Colouring now matches v0.2.0's ground truth within 1 sec on first_child; Morning circle time within ~5 min on last_child (was off by ~minutes-to-hours pre-v0.7.0); circle_time pipeline correctly falls back when model misreads start clock. Wall-clock readings now exposed as separate fields so model misreadings are visible/diagnosable rather than silently corrupting elapsed values. Several iterations explicitly rejected (Shape A/B/C taxonomy, "ignore the clock" prompt approaches, ffmpeg mask). Ground-truth-validated for 3 of 4 videos today; Balloon dance retest deferred to tomorrow (daily RPD quota exhausted).
- **2026-05-20 (latest 2)**: Long-video pipeline cost/time measured (~$1.56, ~12 min per video). Optimization stack documented for the 100-videos-per-day target. Top 3 levers (parallelism across videos, parallel before/after segments, Claude prompt caching) get to ~1.5 h wall-clock and ~$80/day; deferred until needed.
- **2026-05-20 (latest)**: Audio+video patching workflow designed (combine_av.py) for the eventual case where Openhouse records CCTV video and clean audio on separate devices. Three implementation tiers laid out (manual offset → clap sync → cross-correlation). Deferred until dual-source recordings start arriving from the team. Architectural note: this avoids the colleague's recover-from-noise approach (Whisper→Demucs→ElevenLabs) by solving the audio quality problem at the source.
- **2026-05-20 (later)**: Boundary detection design recorded. Chosen approach for v1: single Gemini call per long video to identify first-child and last-child timestamps; then ffmpeg extracts 5-min before/after windows; then existing pipeline scores each. Local CV (YOLO etc.) deferred until scale demands it. Negative finding from a sibling colleague repo confirms GCP Video Intelligence's `PERSON_DETECTION` is unreliable for classroom video and shouldn't be tried.
- **2026-05-20**: First calibration findings recorded — 7 videos × 2 rubrics × model + 3 raters. Five major disagreement patterns identified; Cleanup is the biggest gap. Open question on Video 2 mapping (multisensorybins file vs team's water/fishing notes). Rubric tightening deferred pending higher-level structural discussion.
- **2026-05-18 (later)**: Cost and throughput section added with measured numbers from today's runs. Per-classroom-per-day economics show ours is 3–8× cheaper than the colleague's pipeline at the 20-parent classroom level. Free tier hit hard daily walls on three Gemini models (2.5-flash, 3.1-flash-lite, 3-flash) — paid tier required for any serious testing going forward. Maya case documented as concrete example of "grounded name + interpolated narrative" failure mode that motivates our verbatim-quote evidence requirement.
- **2026-05-18**: Ran the reference repo `nikhil2197/agentic_vid_QA` end-to-end on the same 35-min video. Required: local working copy at `/Users/oh/Desktop/agentic_vid_QA_run`, a parallel `llm_adapter_local.py` using google-genai + Files API (no GCS), `catalog_adapter.py` patched to accept local paths, `video_analyzers.py` chunked to 5-min slices, upload caching, per-chunk persistence, and model swaps. Surfaced concrete defects in their pipeline (logged above under "Reference repo defects"). Confirmed: their *prompts* and *flow shape* are usable as design references; the *plumbing* needs the fixes our own pipeline has already implemented.
- **2026-05-18**: Chunked vision processing implemented (5-min chunks via Gemini `video_metadata` offsets). Resolves the 65 K output token truncation for long videos.
- **2026-05-18**: First real video run (30-min preschool session in Marathi). Three problems surfaced and resolved: (1) Gemini thinking tokens ate output budget → `thinking_budget=0`; (2) transcript ordered first in schema + over-granular → reordered + compact format in vision prompt v0.2; (3) hard truncation at 65 K still possible → `json_repair` fallback. Earlier 1-min synthetic-style real run also revealed activity-type vs rubric mismatch (low scores on dimensions a song can't surface) and audio-nuance loss — both filed under Known limitations for follow-up.
- **2026-05-15**: First version of this doc. Phase 0a project skeleton complete (types, adapters, pipeline modules, score_cli). Smoke test passed on synthetic transcript with positive_climate dimension (score 5, confidence high, evidence verbatim).
- **2026-05-15**: Rubric v0.1 drafted — Pre-K CLASS structure with original anchors. Scoring prompt drafted at `prompts/score_dimension.md`.
- **2026-05-14**: Project planning conversation. Phasing locked. LangGraph rejected for scoring pipeline; reserved for coaching agent. Repo `nikhil2197/agentic_vid_QA` audited as reference.
