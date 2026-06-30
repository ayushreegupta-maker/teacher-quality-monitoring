# Deploying the quality dashboard to Streamlit Community Cloud

This is a one-page recipe for putting the dashboard online behind a shared
password, with videos disabled in the cloud build (they're too big and
contain children's faces — they stay on the team Mac).

## What ends up where

| Lives where | What |
|---|---|
| GitHub repo (public or private) | Code (`frontend/`, `pipeline/`, `prompts/`, `scripts/`), `requirements.txt`, `packages.txt`, `data/tqm_answers.xlsx` (~200 KB, all the Q&A rows), `data/cctv_cameras.xlsx` |
| **NOT in the repo** (gitignored) | `data/sessions/` (trimmed videos), `data/raw/` (NVR pulls), `data/evidence_cache/`, `data/rubric_runs/` (per-run JSON + raw model responses), `.env` |
| Streamlit Cloud Secrets | `password = "..."` and `is_cloud_deploy = true` |

## Steps

### 1. Confirm the repo is push-ready (one-time)

From the repo root:

```bash
git status                                # nothing weird staged
git ls-files | grep -E '\.(mp4|mov)$'    # should be empty
ls requirements.txt packages.txt         # both present
```

If videos show up in `git ls-files`, fix `.gitignore` before pushing.

### 2. Push to GitHub

```bash
git add requirements.txt packages.txt frontend/
git commit -m "Streamlit quality dashboard + cloud deploy files"
git push origin main
```

### 3. Connect Streamlit Community Cloud

1. Sign in at <https://streamlit.io/cloud> with the GitHub account that owns
   the repo.
2. Click **"New app"** → pick the repo, branch (`main`), and main file path
   (`frontend/quality_app.py`).
3. Click **"Advanced settings"** → **"Secrets"** and paste:

   ```toml
   password = "pick-something-strong"
   is_cloud_deploy = true
   ```

4. Click **"Deploy"**. First build takes 2–4 min (installing pandas,
   openpyxl, ffmpeg).
5. Once it's up you'll get a URL like
   `https://<your-handle>-teacher-quality-monitoring-frontend-quality-app.streamlit.app`.

### 4. Share the URL + password with the quality team

The password gate lives at the top of the app. Whoever has the password is
in. To rotate: edit the secret in the Streamlit Cloud settings → restart
the app.

## Notes & gotchas

- **`is_cloud_deploy` flag.** The app reads `st.secrets["is_cloud_deploy"]`
  to decide whether to show the "video disabled" banner and to swap the
  materials-seen tooltip. Locally there's no secrets file, so it defaults
  to False and the app behaves exactly as it does today on your Mac.
- **Auto-redeploy.** Every push to `main` triggers a rebuild. The cache
  invalidates and the new data shows up after ~60 s (the `@st.cache_data`
  TTLs).
- **Updating `tqm_answers.xlsx`.** After each rubric run finishes on your
  Mac, the xlsx grows. To get those new rows onto the cloud dashboard,
  commit + push the updated xlsx:

  ```bash
  git add data/tqm_answers.xlsx
  git commit -m "data: append run for <session-id>"
  git push
  ```

  This is the trade-off for "data lives in the repo" — pushing IS the
  publish step. If this gets annoying, the next move is to host the xlsx
  in cloud storage and have the app fetch it on startup.
- **Materials list in the cloud build.** Currently empty because
  `materials_seen` lives in per-run JSON which is gitignored. Two ways
  to surface it later:
  1. Add a `materials_seen_csv` column to the xlsx (one row per question
     can carry the run-level list — wasteful but simple).
  2. Carve out a `.gitignore` exception for `data/rubric_runs/**/5_answers.json`.

## When to graduate off Community Cloud

The free tier is fine for the quality team pilot. Reasons to move:

- Need per-user audit (who saw what, when)
- Need Google SSO instead of a shared password
- Want videos to play in the hosted dashboard (R2 + signed URLs)
- Need >1 GB RAM (large dataframes, many sessions)

When that day comes the path is roughly: Docker container → small VM
(Hetzner / DO) → Caddy reverse proxy with Google OAuth → Cloudflare R2
for videos. About 1–2 days of work.
