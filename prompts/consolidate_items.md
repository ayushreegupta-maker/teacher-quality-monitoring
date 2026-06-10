---
id: consolidate_items
version: 0.1.0
description: |
  Reads merged visual observations from a play-space recording and produces a
  consolidated inventory of distinct items, materials, and tools observed at
  the activity zone(s). Runs once per video after the vision pass and before
  rubric scoring. Used primarily to give the toy design rubric (Anchor &
  Choice Materials, Self Served) a clean enumerated inventory rather than
  scattered per-frame mentions.
inputs:
  - session metadata (subject, activity_context) injected via Jinja
  - merged visual observations rendered as a string
outputs:
  - JSON object with activity_zone_items[], other_items_in_room[], notes
---

# SYSTEM

You will receive a list of timestamped visual observations from a video of a children's play space or activity. Your job is to extract a CONSOLIDATED INVENTORY of all distinct items, materials, tools, and objects mentioned across the observations.

Rules:
- Focus on items AT or IN the activity zone(s). Things elsewhere in the room (wall decor, fixtures, items on shelves that aren't used in the activity) go in a separate `other_items_in_room` list.
- Deduplicate. If "blocks" appears in 8 observations, list "blocks" once.
- Group items by category where helpful (Materials, Tools, Furniture, Containers, Children-clothing items, etc.).
- Be SPECIFIC where the observations are specific (named colors, types, counts).
- Be VAGUE where the observations are vague — do NOT invent specificity. If observations say "various colors" without naming them, write "unspecified colors" rather than guessing.
- For each item, include a count if explicitly stated. Otherwise use a qualitative quantity ("one", "multiple", "many", "unknown").
- Use "possibly" / "unclear if" when source observations are ambiguous about presence or quantity.

## Output format

Return ONLY valid JSON. No prose. No code fences.

```
{
  "activity_zone_items": [
    {
      "category": "Materials | Tools | Containers | Furniture | Other",
      "name": "<item or material name>",
      "count_or_quantity": "<count if stated, else 'one' | 'multiple' | 'many' | 'unknown'>",
      "specifics": "<details from observations, e.g. 'red, blue, yellow' or 'with embedded small toys'; null if none>",
      "first_seen_at": "<HH:MM:SS of the first observation mentioning it, or null>"
    }
  ],
  "other_items_in_room": [
    {
      "name": "<thing seen in room but NOT at the activity zone>",
      "location": "<wall, shelf, corner, ceiling, etc.; null if not stated>"
    }
  ],
  "notes": "<optional brief notes about ambiguity or limitations of the observations; null if none>"
}
```

# USER

## Session metadata

- **Subject:** {{ session.subject | default("not specified") }}
- **Activity context:** {{ session.activity_context | default("not provided") }}

## Visual observations

{{ observations_rendered }}

Extract the consolidated inventory. Return only the JSON object specified above.
