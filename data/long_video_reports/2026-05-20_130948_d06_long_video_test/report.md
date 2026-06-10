# Long Video Report: D06 long video test

**Source:** `D06_20250919105616.mp4`
**Total duration:** 00:21:42 (1302 sec)
**Boundary detection:** first child at `00:59:19`, last child at `01:15:00` (confidence: **high**)
**Boundary notes:** Children enter the room at 00:59:19 and leave at 01:15:00. The teacher is present throughout this period.
**Before window:** `00:00:00 – 00:05:00` (300 sec)
**After window:** `00:16:42 – 00:21:42` (300 sec)

**Warnings:**
- first_child timestamp (3559s) is beyond video duration (1302s) by more than 5% — likely hallucinated by the boundary model; treating as missing and falling back
- last_child timestamp (4500s) is beyond video duration (1302s) by more than 5% — likely hallucinated by the boundary model; treating as missing and falling back
- No valid child boundary detected — falling back to first 5 min / last 5 min of recording.

---

## Playground — Before vs After

| Dimension | Before | After | Delta |
|---|---|---|---|
| narrative | 0 | 0 | — |
| multi_sensory | 0.5 | 0.5 | — |
| boundary | 0.5 | 1.0 | ↑ 0.50 |
| movement | 0.5 | 1.0 | ↑ 0.50 |
| clean_up | ie | ie | — |
| **Overall** | **0.38** | **0.62** | **↑ 0.25** |

## Toy Design — Before vs After

| Dimension | Before | After | Delta |
|---|---|---|---|
| purpose | 0 | 1.0 | ↑ 1.00 |
| anchor_and_choice_materials | ie | 0.5 | ie → 0.5 |
| spark_curiosity | 0 | 0 | — |
| challenge_adjustment | ie | 0 | ie → 0 |
| self_served | ie | 1.0 | ie → 1.0 |
| **Overall** | **0.00** | **0.50** | **↑ 0.50** |

---

## Per-segment rationale

### Before

**playground** — overall 0.38

- **narrative**: `0` (high)
    - The session shows a generic, undecorated classroom space. The 'colorful decorations' mentioned are never described in thematic terms and do not constitute immersive narrative framing. No props, banners, or deliberate scene-setting elements are observed throughout the recording.
- **multi_sensory**: `0.5` (medium)
    - The session shows a largely bare room with mats being introduced late in the recording. Two senses (visual and tactile via mats) are nominally present, but the setup is minimal and the activity design is not fully observable within the 5-minute clip. No sound-making elements, natural materials, or deliberate multi-sensory arrangements are visible.
- **boundary**: `0.5` (medium)
    - The mats are emerging as boundary markers by the end of the clip, but they are being placed reactively after children have already scattered. The teacher's pointing gesture suggests the boundary is not yet intuitive without adult direction. A score of 0.5 reflects the partial and in-progress nature of the boundary establishment.
- **movement**: `0.5` (medium)
    - The activity setup is still in progress at the end of the recording. Access to the room is clearly unobstructed, but the activity zone and deliberate positioning are only partially established by the end of the 5-minute clip. No materials are visible in use, so reach cannot be assessed. Score of 0.5 reflects clear access but incomplete positioning.
- **clean_up**: `insufficient_evidence` (low)
    - The session recording shows only the initial setup phase — children and teacher have just entered the room and mats are being placed on the floor. No cleanup activity occurs during the 5-minute recording. Storage systems, return locations, labelling, and children's ability to independently return materials cannot be assessed from this footage. The room appears largely bare with no visible storage …

**toy_design** — overall 0.00

- **purpose**: `0` (high)
    - The room has no pre-staged activity materials at any point during the recording. The only materials introduced (blue mats) are placed by the teacher after children have already entered, and even then require adult direction to communicate purpose. No activity zone with a self-evident arrangement is ever established.
- **anchor_and_choice_materials**: `insufficient_evidence` (low)
    - The recording captures an empty room for the first ~3 minutes, then children and a teacher entering and the teacher placing blue mats on the floor. No play or learning materials (tools, sensory bins, art supplies, blocks, etc.) are visible or described at any point during the 5-minute session. The setup described in the activity notes ('exacting setup TBD') was apparently not in place during filmi…
- **spark_curiosity**: `0` (medium)
    - The session recording is very short and the activity setup is still being established at the end of the clip. Only blue mats are visible at the activity area; no curiosity-sparking elements are present. Wall decorations and the blackboard are ambient room features and do not count per rubric rules.
- **challenge_adjustment**: `insufficient_evidence` (low)
    - The recording captures only the very beginning of session setup — children enter at 03:00 and the teacher is still placing mats on the floor at 05:00. No activity materials, difficulty variants, tiered options, or extension materials are visible. The transcript is empty. There is no observable evidence of challenge adjustment design in either direction; the session simply has not progressed far en…
- **self_served**: `insufficient_evidence` (low)
    - The recording shows an essentially empty room for the first 3 minutes, and the final 2 minutes only show children entering and a teacher placing blue mats on the floor. No activity materials are visible — no toys, manipulatives, art supplies, or other learning materials are present or described. There is insufficient evidence to assess whether materials are safe, recognisable, or light enough for …

### After

**playground** — overall 0.62

- **narrative**: `0` (high)
    - The space is a plain classroom with mats, small tables, paper, crayons, and a blackboard. No thematic decor, props, banners, or immersive framing elements are described at any point in the session. Drawing materials laid out for an activity do not constitute narrative immersion per the rubric's common failure modes.
- **multi_sensory**: `0.5` (medium)
    - The setup is primarily a drawing activity with crayons and paper on mats — this meaningfully engages tactile and visual senses. However, the sensory range is narrow (no water, sand, sound-making elements, or other distinct material types visible), and the movement observed appears incidental rather than designed. This places the session at 0.5 rather than 1.0.
- **boundary**: `1.0` (high)
    - The blue mats on a contrasting wooden floor, paired with small wooden tables and paper/crayons, create a well-defined single activity zone. Children naturally orient to and return to this zone, confirming the boundary is self-evident.
- **movement**: `1.0` (high)
    - The activity is a floor-based drawing session with mats and low tables — deliberate positioning is clear. Children move freely throughout the session. A visiting adult stands near the blackboard but does not block child access. Both access and positioning indicators are fully met.
- **clean_up**: `insufficient_evidence` (low)
    - The session recording covers only an active drawing activity with no cleanup phase observed. There is no evidence of children returning materials to storage, no visible storage units, shelving, bins, or labels captured in the visual observations, and no audio related to cleanup. The design of the storage system and whether children can independently return materials cannot be assessed from this fo…

**toy_design** — overall 0.50

- **purpose**: `1.0` (high)
    - The setup — paper and crayons on small tables with children seated on mats — is a classic, unambiguous drawing arrangement. No labels or signs are needed; the material arrangement alone communicates the activity clearly.
- **anchor_and_choice_materials**: `0.5` (medium)
    - The session clearly has a drawing anchor, but the visual descriptions only mention paper and crayons without specifying variety (e.g., multiple crayon colors, different paper sizes, additional tools). The camera/description quality does not allow confirmation of whether meaningful choice variety exists within the materials. Scored 0.5 because an anchor is present but choice material variety cannot…
- **spark_curiosity**: `0` (high)
    - The entire session shows a standard drawing activity with paper and crayons. No unusual, unexpected, or curiosity-sparking elements are visible at or within the activity zone at any point during the recording.
- **challenge_adjustment**: `0` (high)
    - The session shows a uniform drawing activity for all children with identical materials. No tiered versions, optional extensions, or add-on materials are visible at any point. The teacher assists individually but the setup itself offers no deliberate difficulty variation.
- **self_served**: `1.0` (high)
    - The activity uses paper, crayons, small wooden tables, floor mats, and cushions — all standard, child-safe, lightweight, and immediately recognisable materials for 3–5 year olds. Teacher assistance observed is incidental to the design. No heavy, sharp, toxic, or unrecognisable materials are visible.

---

## Items inventory

### Before

- **blue mats** _(Furniture)_ × multiple — blue colored floor mats placed on the floor by the teacher

### After

- **blue mats** _(Furniture)_ × multiple — blue colored, placed on wooden floor for children to sit on
- **small wooden tables** _(Furniture)_ × multiple — small-sized, wooden
- **paper** _(Materials)_ × multiple — used for drawing activity
- **crayons** _(Materials)_ × multiple — used for drawing; specific colors not stated
