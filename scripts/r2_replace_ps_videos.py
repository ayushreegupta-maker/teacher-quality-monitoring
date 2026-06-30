"""
One-off: swap two Public Speaking videos on the Cloudflare R2 bucket.

Delete:
  public_speaking/2026-05-21__D14__1700.mp4
  public_speaking/2026-05-23__D14__1000.mp4

Upload (from the local upload folder):
  public_speaking/2026-05-20__D14__1700.mp4
  public_speaking/2026-05-18__D14__0900.mp4

Net change: −2.2 GB removed, +1.1 GB added. Total R2 usage stays well
under the 10 GB free-tier limit.

Reads R2 credentials from the local .env. Run from the repo root:
    .venv/bin/python scripts/r2_replace_ps_videos.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import boto3
from botocore.config import Config

ROOT = Path(__file__).resolve().parent.parent
UPLOAD_DIR = Path.home() / "Desktop" / "openhouse-tqm-videos-upload"

DELETE_KEYS = [
    "public_speaking/2026-05-21__D14__1700.mp4",
    "public_speaking/2026-05-23__D14__1000.mp4",
]
UPLOAD_KEYS = [
    "public_speaking/2026-05-20__D14__1700.mp4",
    "public_speaking/2026-05-18__D14__0900.mp4",
]


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip().strip("'\""))


def _require(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        sys.exit(f"missing env var {name!r} (set in .env)")
    return v


def main():
    _load_env(ROOT / ".env")
    account = _require("R2_ACCOUNT_ID")
    bucket = _require("R2_BUCKET")
    key = _require("R2_ACCESS_KEY_ID")
    secret = _require("R2_SECRET_ACCESS_KEY")

    client = boto3.client(
        "s3",
        endpoint_url=f"https://{account}.r2.cloudflarestorage.com",
        aws_access_key_id=key,
        aws_secret_access_key=secret,
        region_name="auto",
        config=Config(signature_version="s3v4"),
    )

    # ── 1. Delete the two stale PS objects ──
    print(f"Deleting {len(DELETE_KEYS)} object(s) from {bucket}...\n")
    for k in DELETE_KEYS:
        try:
            client.head_object(Bucket=bucket, Key=k)
            client.delete_object(Bucket=bucket, Key=k)
            print(f"  ✗ {k} — deleted")
        except client.exceptions.ClientError as e:
            code = e.response["Error"]["Code"]
            if code in ("404", "NoSuchKey", "NotFound"):
                print(f"  · {k} — already absent")
            else:
                raise

    # ── 2. Upload the two replacements ──
    print(f"\nUploading {len(UPLOAD_KEYS)} new object(s)...\n")
    for k in UPLOAD_KEYS:
        src = UPLOAD_DIR / k
        if not src.exists():
            print(f"  ! {k} — source missing at {src}; skipping")
            continue
        size_mb = src.stat().st_size / 1024 / 1024
        try:
            head = client.head_object(Bucket=bucket, Key=k)
            if head["ContentLength"] == src.stat().st_size:
                print(f"  ⏭  {k} ({size_mb:.0f} MB) — already on R2 with matching size")
                continue
        except client.exceptions.ClientError as e:
            code = e.response["Error"]["Code"]
            if code not in ("404", "NoSuchKey", "NotFound"):
                raise
        print(f"  ⬆  {k} ({size_mb:.0f} MB) — uploading...")
        client.upload_file(
            Filename=str(src),
            Bucket=bucket,
            Key=k,
            ExtraArgs={"ContentType": "video/mp4"},
        )
        print(f"     done.")

    print("\n✓ swap complete")


if __name__ == "__main__":
    main()
