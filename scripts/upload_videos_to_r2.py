"""
One-shot upload: push the renamed videos in
~/Desktop/openhouse-tqm-videos-upload/ to Cloudflare R2.

Cloudflare R2's web UI tops out at 300 MB per file. The S3-compatible
API (which is what boto3 uses) has no such limit — boto3.upload_file
transparently uses multipart upload for anything large.

Credentials are read from a local .env (gitignored). Required vars:

  R2_ACCOUNT_ID         — 32-hex Cloudflare account id
  R2_BUCKET             — e.g. openhouse-tqm-videos
  R2_ACCESS_KEY_ID      — from the R2 API token
  R2_SECRET_ACCESS_KEY  — from the R2 API token

The .env should sit at the repo root next to .env.example. Never commit it.

Idempotent: each object's ContentLength is fetched first; files already
uploaded with matching size are skipped.

Run from the repo root:
    .venv/bin/python scripts/upload_videos_to_r2.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import boto3
from botocore.config import Config

ROOT = Path(__file__).resolve().parent.parent
SOURCE_DIR = Path.home() / "Desktop" / "openhouse-tqm-videos-upload"


def _load_env_file(path: Path) -> None:
    """Minimal .env loader — just sets os.environ entries for KEY=VALUE
    lines. Skips blanks + comments. Tolerates simple quoted values."""
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        val = val.strip().strip("'\"")
        os.environ.setdefault(key.strip(), val)


def _require_env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        sys.exit(
            f"missing env var {name!r}. "
            "Add it to .env (see .env.example) or export it before running."
        )
    return v


def main():
    _load_env_file(ROOT / ".env")

    account_id = _require_env("R2_ACCOUNT_ID")
    bucket = _require_env("R2_BUCKET")
    access_key = _require_env("R2_ACCESS_KEY_ID")
    secret_key = _require_env("R2_SECRET_ACCESS_KEY")
    endpoint = f"https://{account_id}.r2.cloudflarestorage.com"

    if not SOURCE_DIR.exists():
        sys.exit(f"missing source dir: {SOURCE_DIR}")

    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="auto",
        config=Config(signature_version="s3v4"),
    )

    # Walk SOURCE_DIR — the on-disk path under the upload folder IS the R2 key.
    files: list[tuple[Path, str, int]] = []
    for p in sorted(SOURCE_DIR.rglob("*.mp4")):
        rel = p.relative_to(SOURCE_DIR).as_posix()  # e.g. art/2026-05-18__D28__0900.mp4
        files.append((p, rel, p.stat().st_size))

    if not files:
        sys.exit(f"no .mp4 files under {SOURCE_DIR}")

    total_bytes = sum(s for _, _, s in files)
    print(f"found {len(files)} file(s) — {total_bytes/1024/1024/1024:.2f} GB total")
    print(f"target: {endpoint}/{bucket}/\n")

    skipped = uploaded = 0
    for p, key, size in files:
        size_mb = size / 1024 / 1024
        # Check if already uploaded with the same byte count
        try:
            head = client.head_object(Bucket=bucket, Key=key)
            if head["ContentLength"] == size:
                print(f"⏭  {key} ({size_mb:.0f} MB) — already on R2 with matching size, skipping")
                skipped += 1
                continue
            print(f"↻  {key} ({size_mb:.0f} MB) — exists on R2 but size differs ({head['ContentLength']} vs {size}); re-uploading")
        except client.exceptions.ClientError as e:
            if e.response["Error"]["Code"] not in ("404", "NoSuchKey", "NotFound"):
                raise

        print(f"⬆  {key} ({size_mb:.0f} MB) — uploading...")
        client.upload_file(
            Filename=str(p),
            Bucket=bucket,
            Key=key,
            ExtraArgs={"ContentType": "video/mp4"},
        )
        uploaded += 1
        print(f"   done.")

    print(f"\n✓ upload pass complete — {uploaded} uploaded, {skipped} skipped")


if __name__ == "__main__":
    main()
