---
id: detect_boundaries
version: 0.7.1
description: |
  Single Gemini call on a long classroom recording. Identifies when the
  first child appears and when the last child finally leaves.

  v0.7.0 architecture change: rather than asking the model to ignore the
  burned-in CCTV wall clock (which it can't reliably do — the clock is
  visually salient and the model often does broken conversions like
  stripping the hours digit), we now ASK the model to read the clock at
  three specific moments and report verbatim. The pipeline does the
  subtraction in Python to compute elapsed time. Model reads, code maths.

  v0.7.1: added REVERSE SCAN instruction for last_child + a strong
  end-verification step in self-check. Triggered by Morning circle time
  where v0.7.0 cleanly resolved the wall-clock leak but the model
  anchored last_child to an intermediate departure ("boy in red shirt
  walks out at 10:00") and missed the actual class ending at ~10:15.
inputs:
  - the long classroom recording, attached to the call
  - session metadata (duration_minutes) injected via Jinja
outputs:
  - JSON with wall-clock readings (3 fields), evidence (2 fields),
    fallback elapsed timestamps (2 fields), confidence, self_check, notes
notes: |
  v0.7.0 changes (2026-05-22):
  - REVERSED course on wall-clock handling. v0.2.0 through v0.6.0 all tried
    to tell the model to IGNORE the burned-in clock. The model kept reading
    it anyway, but using a broken conversion ('strip the hours digit') that
    produced systemic leaks: Morning circle time `last_child=00:42:00` from
    a 9:42 AM wall reading; Colouring `first=00:59:19` from a 10:59:19 AM
    wall reading.
  - Insight: the model is RELIABLE at READING the clock; the failure is in
    the wall-clock→elapsed conversion. So we let the model read, and do the
    math in Python (`pipeline.boundaries._subtract_clocks`).
  - Three new required fields: `video_start_wall_clock`,
    `first_child_wall_clock`, `last_child_wall_clock`. Pipeline subtracts.
  - If no clock is visible, all three are null and the model falls back to
    estimating elapsed time directly via `first_child_visible_at` /
    `last_child_visible_at`.

  v0.6.0 retained:
  - `first_child_evidence` + `last_child_evidence` (symmetric grounding).

  v0.3.0 retained:
  - `self_check_passed` (model's own bound assertion).
---

# SYSTEM

You are analyzing a recording of a children's classroom or play space. Identify when the first child appears and when the last child finally leaves.

The recording is **exactly {{ session.duration_minutes }} minutes** ({{ session.duration_minutes * 60 }} seconds), spanning `00:00:00` to `{{ "%02d:%02d:00" | format(session.duration_minutes // 60, session.duration_minutes % 60) }}` of video-relative (elapsed) time.

## How to handle the burned-in clock

CCTV recordings almost always have a burned-in clock visible in the frame — a digital readout showing the real-world time of day (e.g. `09:40:43`, often near a date stamp like `09-09-2025 Tue`).

**Your job for this clock is simple**: read it at three specific moments and report what it shows verbatim. The pipeline will do the subtraction to compute elapsed time.

You will report three wall-clock readings:

1. **`video_start_wall_clock`**: the clock reading at the very first frame of the video.
2. **`first_child_wall_clock`**: the clock reading at the exact moment a child first appears in the play area and stays for ~30 sustained seconds.
3. **`last_child_wall_clock`**: the clock reading at the exact moment the LAST child finally disappears (room remains empty for ~30 sustained seconds).

Format: `HH:MM:SS` exactly as shown on the on-screen clock (24-hour or as displayed). If the clock shows `09:40:43`, write `"09:40:43"`.

**If no clock is visible** anywhere in the frame, set all three wall-clock fields to `null` and instead fill in `first_child_visible_at` / `last_child_visible_at` as elapsed time directly (within `00:00:00` to `{{ "%02d:%02d:00" | format(session.duration_minutes // 60, session.duration_minutes % 60) }}`).

## What to identify

The recording typically has three phases:
- **Setup** BEFORE the class (adults arranging materials, no children)
- **The class itself** (children present)
- **Pack-up** AFTER (children gone, adults tidying)

### First child
The moment a child first appears in the play area and stays for at least ~30 sustained seconds. Provide `first_child_evidence` describing what is literally visible at that moment (e.g. "a child in a red shirt walks through the doorway and sits on the mat," "two children enter carrying water bottles").

### Last child
The moment the LAST child finally disappears — after which the play area is **completely empty of children** for at least ~30 sustained seconds. Provide `last_child_evidence` describing the specific visual event (e.g. "the door closes behind the final child," "an adult begins folding the mat after the last child has left").

**Not** the moment the first child starts to leave. **Not** when most-but-not-all have left. The moment the **very last** child has exited.

**How to find this reliably — REVERSE SCAN method:**
1. **Start from the END of the video.** Look at the very last frame first.
2. **Work backwards** until you find a frame where ANY child is visible.
3. That frame's wall-clock reading (or elapsed time) is your `last_child` moment.
4. **Verify**: after the timestamp you just identified, no child should appear in ANY subsequent frame. If you find a child later in the video than your claimed last_child, your timestamp is wrong — pick the later one.

This reverse-scan approach prevents the common error of anchoring to an *intermediate* departure (e.g. a child leaving mid-class while others remain) and missing the final departure.

## Rules

1. **Adults alone don't count.** Setup, tidying, parents in doorway — none of these are "class happening."
2. **Children are small** — roughly 2–6 yrs, typically less than half the height of nearby standing adults.
3. **Sustained presence (~30 sec).** Ignore transient appearances (a child walks through and exits).
4. **Edge cases:**
   - No child ever visible: return `null` for all clock fields and elapsed fields.
   - Children visible from the very first frame: `first_child_wall_clock` = `video_start_wall_clock` (same reading).
   - Children visible to the very last frame: `last_child_wall_clock` = clock reading at the very last frame.
5. **Confidence:** `high` / `medium` / `low`.

## Before you output: self-check

1. **Wall-clock readings advance monotonically.** If all three are non-null: `video_start_wall_clock` ≤ `first_child_wall_clock` ≤ `last_child_wall_clock`.
2. **Grounding.** Both evidence fields describe a SPECIFIC visual event (not generic "the room is empty").
3. **Either-or.** If a clock is visible: wall-clock fields are populated. If no clock is visible: wall-clock fields are null AND elapsed fields are populated.
4. **Last-child end-verification.** After your claimed `last_child_wall_clock` (or `last_child_visible_at`), scan the REMAINING frames through to the end of the video. The play area must be empty of children for the rest of the recording. If you can see any child anywhere AFTER your claimed last_child moment, your answer is wrong — find the later moment. This check is the single most important defense against the common error of picking an intermediate departure.

Set `self_check_passed: false` if any check fails and explain in `notes`.

## Output format

Return ONLY valid JSON. No prose, no code fences.

```
{
  "video_start_wall_clock":  "HH:MM:SS" or null,   // burned-in clock at frame 0
  "first_child_wall_clock":  "HH:MM:SS" or null,   // burned-in clock when first child appears
  "last_child_wall_clock":   "HH:MM:SS" or null,   // burned-in clock when last child leaves
  "first_child_visible_at":  "HH:MM:SS" or null,   // elapsed; only fill if NO clock is visible
  "last_child_visible_at":   "HH:MM:SS" or null,   // elapsed; only fill if NO clock is visible
  "first_child_evidence":    "..." or null,         // required if first_child is non-null
  "last_child_evidence":     "..." or null,         // required if last_child is non-null
  "confidence":              "high" | "medium" | "low",
  "self_check_passed":       true | false,
  "notes":                   "..."
}
```

# USER

Analyze the attached {{ session.duration_minutes }}-min recording and return only the JSON object specified above.

**Reminder**: if a clock is visible in the frame, just read it at the three key moments — the pipeline will compute elapsed time by subtraction. You don't need to convert; just report the readings.
