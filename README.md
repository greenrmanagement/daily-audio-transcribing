# Daily Audio Transcribing

Automatically transcribes new audio files dropped into a **shared Google Drive
folder** and saves each transcript back into the **same folder** as a Google Doc
— once per day, unattended.

## How it works

```
GitHub Actions (daily cron)
  └─ transcribe.py
       1. Authenticate to Google Drive with a service account
       2. List audio files in the shared folder
       3. Skip any that already have a "<name> - Transcript" Doc  ← "new file" detection
       4. Download each new file → transcribe locally with faster-whisper (Whisper)
       5. Clean up / format / summarize the transcript with the Claude API
       6. Create the formatted Google Doc in the same folder
```

### A note on transcription

The **Claude API cannot process audio directly** — Claude models accept text,
images, and PDFs, not `.mp3`/`.wav`/`.m4a`. So the audio→text step is done by
**[faster-whisper](https://github.com/SYSTRAN/faster-whisper)**, an open-source
Whisper implementation that runs locally inside the GitHub Actions job (no
transcription API key, no per-minute cost). Claude then does what it's great at:
turning the raw, unpunctuated transcript into a clean, paragraphed, summarized
Google Doc.

---

## Setup

### 1. Google Cloud service account

1. In the [Google Cloud Console](https://console.cloud.google.com/), create (or
   pick) a project and **enable the Google Drive API**.
2. **Create a service account** → **Keys** → **Add key** → **JSON**. Download the
   key file.
3. **Share your Drive folder with the service account.** Open the folder in
   Drive, click **Share**, and add the service account's email
   (`something@your-project.iam.gserviceaccount.com`) with **Editor** access
   (Editor is required so it can create the transcript Docs).
   - Works with both a regular shared folder and a Shared Drive.
4. **Get the folder ID** — it's the last part of the folder URL:
   `https://drive.google.com/drive/folders/<THIS_IS_THE_FOLDER_ID>`.

### 2. Anthropic API key

Get a key from the [Claude Console](https://console.anthropic.com/).

### 3. Configure the repository

In **Settings → Secrets and variables → Actions**:

**Secrets** (encrypted):

| Secret | Value |
| --- | --- |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Paste the **entire contents** of the downloaded JSON key file |
| `ANTHROPIC_API_KEY` | Your Claude API key |

**Variables** (plain, non-secret):

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `DRIVE_FOLDER_ID` | ✅ | — | The shared folder's ID |
| `WHISPER_MODEL` | | `small` | `tiny` / `base` / `small` / `medium` / `large-v3` (larger = more accurate, slower) |
| `CLAUDE_MODEL` | | `claude-opus-4-8` | Claude model for formatting |
| `LOOKBACK_DAYS` | | (all) | Only process files created in the last N days |
| `LANGUAGE` | | (auto) | Force a language code, e.g. `en` |

### 4. Run it

- The workflow runs automatically every day at **07:00 UTC** (edit the `cron` in
  `.github/workflows/daily-transcribe.yml` to change the time).
- To test immediately: **Actions → Daily audio transcription → Run workflow**.

---

## Behavior notes

- **Idempotent:** a file is transcribed only if a `<name> - Transcript` Doc
  doesn't already exist in the folder, so re-running never duplicates work.
- **Fault-tolerant:** if Claude formatting fails, the raw transcript is still
  saved; one bad file doesn't stop the rest of the run.
- **Safety cap:** at most `MAX_FILES_PER_RUN` (default 25) files per run, to avoid
  a huge accidental backfill. Change via a repo variable of the same name.

## Run locally

```bash
pip install -r requirements.txt
sudo apt-get install -y ffmpeg          # or: brew install ffmpeg

export GOOGLE_SERVICE_ACCOUNT_JSON=./service-account.json   # path or raw JSON
export DRIVE_FOLDER_ID=your_folder_id
export ANTHROPIC_API_KEY=sk-ant-...
python transcribe.py
```

## Files

| File | Purpose |
| --- | --- |
| `transcribe.py` | The pipeline (Drive I/O, Whisper, Claude formatting) |
| `.github/workflows/daily-transcribe.yml` | Daily schedule + manual trigger |
| `requirements.txt` | Python dependencies |
