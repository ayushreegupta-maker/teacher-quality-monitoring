"""
Diagnostic: ask Gemini specific questions about specific timestamps in the
trimmed video, with NO rubric scaffolding. If the answers differ and look
real, the model is seeing the video. If they all parrot the same template,
the model isn't actually sampling those moments.

Reveals usage_metadata.prompt_token_count too — back-calculates effective fps.
"""
from __future__ import annotations
from pathlib import Path
import json
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from adapters.llm import LLMAdapter

VIDEO = Path("data/sessions/art/2026-05-18__D28__0900/3_trimmed.mp4")

PROBES = [
    "At video offset 00:05:00 (wall clock ~09:18), describe in 2 sentences exactly what the teacher and children are doing. Mention specific objects on the table, the teacher's posture, and how many children are visible.",
    "At video offset 00:45:00 (wall clock ~09:58), describe in 2 sentences exactly what the teacher and children are doing. Mention specific objects on the table, the teacher's posture, and how many children are visible.",
    "At video offset 01:20:00 (wall clock ~10:33), describe in 2 sentences exactly what the teacher and children are doing. Mention specific objects on the table, the teacher's posture, and how many children are visible.",
    "At video offset 01:45:00 (wall clock ~10:58), describe in 2 sentences exactly what the teacher and children are doing. Mention specific objects on the table, the teacher's posture, and how many children are visible.",
]

def main():
    llm = LLMAdapter()
    print(f"Uploading {VIDEO} ...")
    vf = llm.upload_video(VIDEO)
    print(f"Uploaded: {vf.uri}\n")

    for i, p in enumerate(PROBES, 1):
        print(f"━━━ probe {i} ━━━")
        print(f"Q: {p}")
        # Don't force JSON — we want free text describing the scene
        out = llm.call_gemini_video(
            prompt=p,
            video_file=vf,
            force_json=False,
            media_resolution="low",   # match what scored badly
        )
        print(f"A: {out.strip()}\n")

if __name__ == "__main__":
    main()
