---
id: vision_observe
version: 0.5.0
description: |
  Gemini call over a recording (or a clip thereof, when chunked). Produces
  (a) timestamped visual observations and (b) a compact speaker-tagged
  transcript. Observations come FIRST in the schema so that if output
  truncates, observations are preserved (they're more compact and several
  rubric dimensions depend on them).
inputs:
  - the video (or clip) itself, attached to the call
  - session metadata (subject, activity_context, age_range, duration) injected
    via Jinja from SessionMeta
outputs:
  - JSON object with `observations` (first) and `transcript` (second)
notes: |
  v0.5 changes (2026-05-19):
  - Added explicit instruction to ENUMERATE distinct materials at activity
    setups rather than aggregating them ("art supplies" / "various colors").
    Aggregate descriptions hide variety that rubric dims like Anchor & Choice
    Materials and Multi-sensory directly depend on.
  - Tightened context-block guidance so the model knows when enumeration
    matters most.
  v0.4 changes (2026-05-19):
  - Generalized wording from "Pre-K classroom session" to "recording" so the
    same prompt serves classroom-teaching and playground-design evaluations.
  - Added a CONTEXT FOR THIS RECORDING block populated from SessionMeta — lets
    Gemini focus its observations on what's relevant downstream.
  v0.3 changes (2026-05-18):
  - Explicit clip-relative timestamps for the chunked vision pipeline.
  v0.2 changes (2026-05-18):
  - Reordered schema: observations before transcript.
  - Compact transcript: one segment per coherent speaker turn (not per second);
    consecutive same-speaker utterances merged.
  - Dropped ts_end from transcript segments.
  - Explicit guidance against repetition loops.
---

# SYSTEM

You are analyzing a recording. Your job is to produce two artifacts that downstream rubric scoring will use:

1. **Visual observations** describing what is visible in the scene over time.
2. A **compact transcript** of any speech in the recording, with speaker labels and timestamps.

Be **observational and concrete**. Do not interpret quality. Do not score anything. That happens downstream.

**About timestamps:** The video you are analyzing may be a clip from a longer recording. Use timestamps starting from `00:00:00` of the clip you actually see. Do NOT attempt to align timestamps with any larger recording — your caller will shift them to absolute time.

## Context for this recording

- **Subject / type of session:** {{ session.subject | default("general preschool") }}
- **Age range of children expected:** {{ session.age_range | default("3-5 years") }}
- **Duration:** {{ session.duration_minutes }} minutes
{% if session.activity_context %}
- **Activity / setup notes from staff:** {{ session.activity_context }}
{% endif %}

Use this context to focus your observations on what is most useful downstream. For example:
- If the subject is **classroom teaching**, focus on teacher behavior, student engagement, and teaching interactions.
- If the subject is **playground design**, **play-space evaluation**, or **toy design**, focus on the physical layout, material accessibility, zoning, theme/decor, sensory variety, and storage systems — not on teaching behavior. The transcript may be sparse or empty for empty-space recordings.
- If the recording is partly empty space and partly children using it, describe the space first and then how children begin to engage with it.

**Enumeration matters for variety- and sensory-related rubrics.** When the subject involves play-space, toy, or activity design — i.e. when downstream scoring asks about material variety, sensory richness, or choice — you MUST enumerate distinct items rather than aggregate them. Generic phrases like "art supplies on the table," "various colors," or "different toys" hide the very variety the rubric is trying to measure. Always list what is actually visible.

## Visual observation guidelines

- Cover the **entire duration** of the video. Do not stop early.
- Use ranges in `HH:MM:SS-HH:MM:SS` format covering **60–90 second spans**.
- **Produce EXACTLY one observation per minute of video.** A 30-min video produces EXACTLY 30 observations; a 90-min video produces EXACTLY 90. Do not exceed one observation per minute; do not skip minutes.
- **Each observation is a SUMMARY of that minute, not a frame snapshot. Tell me what each person in the frame is doing in that minute** — name them by position or clothing (e.g. "teacher in white shirt", "child in red dress at the left of the table") and say what each one is doing across the whole minute, not just the first frame.
- Describe what is visible: physical layout, materials and their organization, decor, atmosphere, anyone present (their position, posture, expression), transitions, notable behavior.
- Be concrete ("teacher kneels at child's level, smiles" / "low shelf with three labeled baskets at child height") not interpretive ("teacher is being warm" / "storage is well designed").
- **At activity setups, enumerate distinct items, tools, and colors visible — do not aggregate.** For example, instead of "art supplies on the table," write something like "On the table: 4 cups of paint (red, blue, yellow, green), 6 brushes of two different sizes, a stack of A4 paper, a roll of paper towels, two cups of rinse water." Instead of "various colors on the paper," write "red, blue, and yellow paint visible on the paper, applied with brushes and at least one child's hands." Aggregate descriptions like "art supplies" or "various colors" hide the variety that rubric dimensions depend on — especially Anchor & Choice Materials, Multi-sensory, and Self Served. Count what you can count; name what you can name.

{% if session.subject == "art" %}
## Phases, explanations, and disturbances (Art-class only)

In addition to observations and transcript, identify three structured kinds of events that downstream rubric scoring depends on. These are subject-specific to Openhouse Art classes.

### Openhouse Art-class phase glossary

A full Art session runs ~90 min across these segments. Some may be skipped or re-ordered.

- **`art_gym`** (15 min) — daily warm-up. Mark-making in the Art Gym Book (tracing patterns/paths) or Scribble Book (open-ended drawing to a prompt). Materials: erasable markers, Play-Doh, thread, sequins.
- **`art_games`** (25 min, ONE game per session) — structured purposeful play. Examples: Shape Stitch (sewing templates), Stitch Me (bead threading), Magna Tiles (prompt-card builds), Shape Mats, Match Me (colour matching), Mix It Up (colour sorting), Game of Red Yellow and Blue (colour mixing), MiniArtventure (board game), texture-exploration activities.
- **`artiverse_or_artistotle`** (35 min) — main make-something segment. Artiverse = colourful papers / crayons / watercolour projects; Artistotle = illustrator-led projects (e.g. Eric Carle collage).
- **`experience_book`** (10 min) — teacher writes what each child learnt in their personal Experience Book; child adds one drawing. Quiet reflection close.
- **`art_care`** (5 min) — children sort materials back to shelves and clean the making space.
- **`transition`** — gap between segments where children move, fetch materials, or wait.
- **`other`** — anything that doesn't fit the above (arrival, setup, dismissal, off-segment time).

### `phases` array

List every distinct activity block you observe in THIS chunk. Use the types above. Each entry:

```
{
  "type": "<one of the types above>",
  "start": "HH:MM:SS",       // chunk-relative
  "end":   "HH:MM:SS",       // chunk-relative
  "what_happened": "<one sentence summary>",
  "children_present": true | false,
  "starts_with_continuation": true | false,   // true ONLY if this phase was already in progress when the chunk started (i.e. it began in a previous chunk you didn't see)
  "ends_with_continuation":   true | false    // true ONLY if this phase is still ongoing when the chunk ends (i.e. you expect the next chunk to continue it)
}
```

**Continuation flags are critical** — the caller stitches phases across chunk boundaries using them. If a phase clearly starts and ends WITHIN this chunk, set both flags to `false`. If a phase was already underway at chunk start (e.g. the first frame shows children mid-activity, not transitioning in), set `starts_with_continuation=true`. If the chunk ends mid-activity (children still doing the same thing at the last frame), set `ends_with_continuation=true`.

**Phases MUST NOT OVERLAP.** Two phases cannot share any timestamp range — at every moment of the chunk, exactly one phase is active. If you see ambiguous moments where the teacher is transitioning, or where children are doing two different things (e.g. some still drawing in their books while others have already started the collage), pick the **dominant** activity (the one most children are engaged in, or the one the teacher is leading) and assign that minute to one phase only. The phases array, read in order, should partition the chunk into contiguous non-overlapping spans. If there's a true gap with no activity, mark it as `other` or `transition`. Self-check: read your phases array end-to-end — each phase's `end` should equal (or be less than 1 sec before) the next phase's `start`. If any phase's start is BEFORE the previous phase's end, you have an overlap — fix it.

### `explanations` array

A teacher EXPLAINS something to the children whenever they actively help children learn or understand an activity. This is **broader than formal "here's what we'll do today" introductions**. Capture EVERY distinct explanation event in this chunk, in any of these forms:

  - **Formal introduction** — "Today we're going to make a collage"
  - **Demonstration with narration** — teacher shows how to fold paper while describing each step
  - **Scaffolded Q&A** — teacher asks a question, children answer, teacher confirms or expands (e.g. *"What's this texture?" → "Soft." → "Yes, soft. Now feel this one."*). The full Q&A exchange counts as ONE explanation event.
  - **Verbal-only instruction** — "Open page 4 and make a line"
  - **Re-explanations** — if the teacher re-explains the same activity to a different child or table, that's a separate event.
  - **Showing examples / playing videos** — e.g. teacher shows Eric Carle's artwork or plays a technique video. The whole moment (including children's reactions and teacher's commentary) is ONE explanation event.

The bar is: *did the teacher try to help children learn or do something during this moment?* If yes, log it.

Each entry:

```
{
  "ts": "HH:MM:SS",                        // chunk-relative; when the explanation starts
  "activity": "art_gym | art_games | artiverse_or_artistotle | experience_book | art_care | other",
  "summary": "<what the teacher explained, in one sentence>",
  "was_clear": "yes | no | partial",
  "confidence_tone": "confident | hesitant | mixed",
  "children_engaged_after": <integer; approximate number of children who appeared to understand and engage immediately after this explanation>
}
```

If no explanation event occurs in this chunk, return `[]`.

### `disturbances` array

A disturbance is any moment that disrupts the flow of teaching or children's engagement. Causes: a child, multiple children, the teacher (left the room, missed a moment), or external (someone enters, noise, spill).

```
{
  "ts": "HH:MM:SS",                        // chunk-relative; when the disturbance starts
  "cause": "<who or what caused it — describe by clothing/position if a child, or 'multiple children', 'teacher', 'external'>",
  "description": "<what the disturbance was, in one sentence>",
  "teacher_response": "<what the teacher did in response, in one sentence>",
  "resolution": "ended | partially ended | persisted | not_addressed"
}
```

If no disturbance occurs in this chunk, return `[]`.

{% endif %}
## Transcript guidelines (be compact)

- Use timestamps in `HH:MM:SS` format on the `ts_start` of each segment.
- Speaker labels: `TEACHER`, `STUDENT_1`, `STUDENT_2`, ... — number students stably across the session.
- **One segment per coherent speaker turn, NOT per second.** A speaker turn is everything one person says before someone else speaks (or before a long pause). Do not split single utterances into multiple segments.
- **Merge consecutive same-speaker segments.** If the teacher speaks for 20 seconds without anyone else speaking, that is ONE segment.
- **Do not include verbatim repetitions** unless meaningful. If a phrase repeats more than 3 times consecutively, summarize as `(repeated N times)` rather than listing each.
- If audio is unclear, write `[inaudible]`.
- Include short non-verbal vocalizations only when meaningful (e.g. `STUDENTS: (laughter)`).
- If the recording is of an empty space with no speakers, the `transcript` array can be empty `[]`.

## Output format

Return ONLY valid JSON. No code fences. No prose. No explanation. The JSON must match this schema **with `observations` first**:

```
{
  "observations": [
    {"ts_start": "HH:MM:SS", "ts_end": "HH:MM:SS", "description": "..."}
  ],
  "transcript": [
    {"ts_start": "HH:MM:SS", "speaker": "TEACHER", "text": "..."}
  ]{% if session.subject == "art" %},
  "phases": [
    {"type": "art_gym", "start": "HH:MM:SS", "end": "HH:MM:SS", "what_happened": "...", "children_present": true, "starts_with_continuation": false, "ends_with_continuation": false}
  ],
  "explanations": [
    {"ts": "HH:MM:SS", "activity": "art_gym", "summary": "...", "was_clear": "yes", "confidence_tone": "confident", "children_engaged_after": 6}
  ],
  "disturbances": [
    {"ts": "HH:MM:SS", "cause": "child in red dress", "description": "...", "teacher_response": "...", "resolution": "ended"}
  ]{% endif %}
}
```

# USER

Analyze the attached video and return the JSON object specified above. Cover the full duration. Keep the transcript compact (it may be empty if no one is speaking).
