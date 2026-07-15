#!/usr/bin/env python3
"""Daily audio transcription pipeline.

Fetches new audio files from a shared Google Drive folder, transcribes them
locally with faster-whisper (open-source Whisper), cleans/formats the transcript
with the Claude API, and writes the result back to the SAME folder as a Google
Doc.

"New" files are detected idempotently: an audio file is processed only if a
Google Doc named "<audio name>{TRANSCRIPT_SUFFIX}" does not already exist in the
folder. Re-running the job never re-transcribes or duplicates work.

Configuration is entirely via environment variables (see README.md):

    GOOGLE_SERVICE_ACCOUNT_JSON  Service-account key. Either the raw JSON string
                                 or a path to a .json file.
    DRIVE_FOLDER_ID              ID of the shared Drive folder to watch.
    ANTHROPIC_API_KEY            Claude API key.
    WHISPER_MODEL                faster-whisper model size (default: "small").
    CLAUDE_MODEL                 Claude model id (default: "claude-opus-4-8").
    LOOKBACK_DAYS                Only consider files created in the last N days
                                 (default: unset = all untranscribed files).
    TRANSCRIPT_SUFFIX            Doc name suffix (default: " - Transcript").
    MAX_FILES_PER_RUN            Safety cap on files per run (default: 25).
    LANGUAGE                     Force a language code (e.g. "en"); default auto.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import os
import sys
import tempfile

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

# Drive scope: read audio + create the transcript Doc in the same folder.
SCOPES = ["https://www.googleapis.com/auth/drive"]

# Extensions Whisper can handle. Drive's audio/* mimeType is the primary filter;
# this list catches files uploaded with a generic mimeType.
AUDIO_EXTENSIONS = {
    ".mp3", ".wav", ".m4a", ".flac", ".ogg", ".oga",
    ".aac", ".mp4", ".webm", ".mpeg", ".mpga", ".opus",
}

GOOGLE_DOC_MIMETYPE = "application/vnd.google-apps.document"


def env(name: str, default: str | None = None, required: bool = False) -> str | None:
    value = os.environ.get(name, default)
    if required and not value:
        sys.exit(f"ERROR: required environment variable {name} is not set.")
    return value


def log(msg: str) -> None:
    print(msg, flush=True)


# --------------------------------------------------------------------------- #
# Google Drive
# --------------------------------------------------------------------------- #

def build_drive_service():
    raw = env("GOOGLE_SERVICE_ACCOUNT_JSON", required=True)
    if os.path.isfile(raw):
        creds = service_account.Credentials.from_service_account_file(raw, scopes=SCOPES)
    else:
        info = json.loads(raw)
        creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _list_folder(drive, folder_id: str, query_extra: str = "") -> list[dict]:
    """List non-trashed files directly inside a folder (Shared Drive aware)."""
    files: list[dict] = []
    page_token = None
    query = f"'{folder_id}' in parents and trashed = false{query_extra}"
    while True:
        resp = (
            drive.files()
            .list(
                q=query,
                fields="nextPageToken, files(id, name, mimeType, createdTime, fileExtension)",
                pageSize=200,
                pageToken=page_token,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )
        files.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return files


def _is_audio(f: dict) -> bool:
    if (f.get("mimeType") or "").startswith("audio/"):
        return True
    name = f.get("name", "").lower()
    return any(name.endswith(ext) for ext in AUDIO_EXTENSIONS)


def find_new_audio(drive, folder_id: str) -> list[dict]:
    """Audio files in the folder that don't yet have a transcript Doc."""
    all_files = _list_folder(drive, folder_id)

    suffix = env("TRANSCRIPT_SUFFIX", " - Transcript")
    existing_docs = {
        f["name"]
        for f in all_files
        if f.get("mimeType") == GOOGLE_DOC_MIMETYPE and f["name"].endswith(suffix)
    }

    lookback_days = env("LOOKBACK_DAYS")
    cutoff = None
    if lookback_days:
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=int(lookback_days))

    new_files = []
    for f in all_files:
        if not _is_audio(f):
            continue
        if f"{f['name']}{suffix}" in existing_docs:
            continue
        if cutoff is not None:
            created = dt.datetime.fromisoformat(f["createdTime"].replace("Z", "+00:00"))
            if created < cutoff:
                continue
        new_files.append(f)

    new_files.sort(key=lambda f: f.get("createdTime", ""))
    return new_files


def download_audio(drive, file_id: str, name: str, dest_dir: str) -> str:
    path = os.path.join(dest_dir, name)
    request = drive.files().get_media(fileId=file_id, supportsAllDrives=True)
    with io.FileIO(path, "wb") as fh:
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
    return path


def create_google_doc(drive, folder_id: str, title: str, text: str) -> str:
    """Upload plain text as a native Google Doc in the given folder."""
    media = MediaIoBaseUpload(
        io.BytesIO(text.encode("utf-8")),
        mimetype="text/plain",
        resumable=False,
    )
    metadata = {
        "name": title,
        "mimeType": GOOGLE_DOC_MIMETYPE,
        "parents": [folder_id],
    }
    doc = (
        drive.files()
        .create(
            body=metadata,
            media_body=media,
            fields="id, webViewLink",
            supportsAllDrives=True,
        )
        .execute()
    )
    return doc.get("webViewLink", doc["id"])


# --------------------------------------------------------------------------- #
# Transcription (local Whisper)
# --------------------------------------------------------------------------- #

def load_whisper():
    from faster_whisper import WhisperModel

    model_size = env("WHISPER_MODEL", "small")
    log(f"Loading faster-whisper model '{model_size}' (int8 CPU)...")
    # int8 keeps memory/CPU low enough for GitHub-hosted runners.
    return WhisperModel(model_size, device="cpu", compute_type="int8")


def transcribe_audio(model, path: str) -> str:
    language = env("LANGUAGE") or None
    segments, info = model.transcribe(path, language=language, vad_filter=True)
    log(f"  detected language: {info.language} (p={info.language_probability:.2f})")
    return " ".join(seg.text.strip() for seg in segments).strip()


# --------------------------------------------------------------------------- #
# Claude cleanup / formatting
# --------------------------------------------------------------------------- #

def format_with_claude(raw_transcript: str, source_name: str) -> str:
    if not raw_transcript:
        return "(No speech detected in this audio file.)"

    import anthropic

    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    model = env("CLAUDE_MODEL", "claude-opus-4-8")

    system = (
        "You are a transcript editor. You are given a raw, unpunctuated speech-to-text "
        "transcript. Produce a clean, readable version. Rules:\n"
        "- Fix punctuation, capitalization, and obvious transcription errors.\n"
        "- Break the text into logical paragraphs.\n"
        "- If multiple speakers are clearly distinguishable, label them as Speaker 1, "
        "Speaker 2, etc. If you cannot tell, do not invent speakers.\n"
        "- Do NOT summarize away content or drop information; preserve everything said.\n"
        "- Start the document with a short 'Summary' section (2-4 bullet points), then a "
        "'Transcript' heading followed by the cleaned transcript.\n"
        "- Output plain text only (no markdown code fences)."
    )
    user = f"Source audio file: {source_name}\n\nRaw transcript:\n\n{raw_transcript}"

    try:
        # Stream so long transcripts don't hit HTTP timeouts.
        with client.messages.stream(
            model=model,
            max_tokens=32000,
            system=system,
            messages=[{"role": "user", "content": user}],
        ) as stream:
            message = stream.get_final_message()
        parts = [b.text for b in message.content if b.type == "text"]
        formatted = "\n".join(parts).strip()
        return formatted or raw_transcript
    except Exception as exc:  # noqa: BLE001 — never lose a transcript to a format error
        log(f"  WARNING: Claude formatting failed ({exc}); saving raw transcript.")
        return f"(Automatic formatting unavailable — raw transcript below.)\n\n{raw_transcript}"


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main() -> int:
    folder_id = env("DRIVE_FOLDER_ID", required=True)
    suffix = env("TRANSCRIPT_SUFFIX", " - Transcript")
    max_files = int(env("MAX_FILES_PER_RUN", "25"))

    drive = build_drive_service()
    new_files = find_new_audio(drive, folder_id)

    if not new_files:
        log("No new audio files to transcribe. Done.")
        return 0

    if len(new_files) > max_files:
        log(f"Found {len(new_files)} new files; capping this run at {max_files}.")
        new_files = new_files[:max_files]

    log(f"Transcribing {len(new_files)} new audio file(s)...")
    model = load_whisper()

    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        for f in new_files:
            name = f["name"]
            log(f"\n=> {name}")
            try:
                path = download_audio(drive, f["id"], name, tmp)
                raw = transcribe_audio(model, path)
                formatted = format_with_claude(raw, name)
                link = create_google_doc(drive, folder_id, f"{name}{suffix}", formatted)
                log(f"  saved Google Doc: {link}")
                try:
                    os.remove(path)
                except OSError:
                    pass
            except Exception as exc:  # noqa: BLE001 — one bad file shouldn't stop the run
                failures += 1
                log(f"  ERROR processing {name}: {exc}")

    log(f"\nDone. {len(new_files) - failures} succeeded, {failures} failed.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
